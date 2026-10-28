"""Migration 0005_billing: existing subscriptions are backfilled ``legacy``,
every later insert defaults to ``psp`` (fail closed), and a downgrade refuses
to erase intent-less markers unless forced (ADR-0003 open item 9)."""

import uuid
from typing import Any

import pytest

from sandloom.sdk.testing import migrate_module, run_sql, upgrade_core_chain


def _billing_to(url: str, revision: str) -> None:
    """Move billing's chain to ``revision`` (``-rev``: downgrade to it)."""
    if revision.startswith("-"):
        migrate_module(url, "sandloom_billing", revision[1:], downgrade=True)
    else:
        migrate_module(url, "sandloom_billing", revision)


def _insert_subscription(url: str) -> str:
    customer = str(uuid.uuid4())
    run_sql(
        url,
        "INSERT INTO billing_customers (id, user_id) VALUES (%s, %s)",
        (customer, str(uuid.uuid4())),
    )
    sub_id = f"sub_{uuid.uuid4().hex[:8]}"
    run_sql(
        url,
        "INSERT INTO billing_subscriptions (customer_id, stripe_subscription_id, "
        "status, cancel_at_period_end) VALUES (%s, %s, 'active', false)",
        (customer, sub_id),
    )
    return sub_id


def _origin(url: str, sub_id: str) -> Any:
    return run_sql(
        url,
        "SELECT origin FROM billing_subscriptions WHERE stripe_subscription_id = %s",
        (sub_id,),
    )[0][0]


def test_backfill_legacy_then_fail_closed_default(
    scratch_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = scratch_database
    upgrade_core_chain(url)  # core chain only
    _billing_to(url, "0004_billing")
    before = _insert_subscription(url)

    _billing_to(url, "head")
    after = _insert_subscription(url)  # an insert that names no origin

    assert _origin(url, before) == "legacy"
    assert _origin(url, after) == "psp"

    # A downgrade would erase the psp marker (a later upgrade relabels it
    # legacy = granting): refused unless explicitly forced.
    with pytest.raises(RuntimeError, match="intent-less"):
        _billing_to(url, "-0004_billing")
    monkeypatch.setenv("SANDLOOM_BILLING_DROP_ORIGIN", "1")
    _billing_to(url, "-0004_billing")
