"""Migration 0008_billing: credit grants, negative balances allowed (ADR-0003
open item 12)."""

import uuid

import pytest

from sandloom.sdk.testing import run_sql, upgrade_core_chain
from sandloom_billing.tests.test_migration_subscription_origin import _billing_to

# Slow (walks billing's migration chain up and down): skipped by the fast dev loop `pytest -m "not slow"`.
pytestmark = pytest.mark.slow


def test_negative_balance_is_allowed_and_blocks_the_downgrade(
    scratch_database: str,
) -> None:
    url = scratch_database
    upgrade_core_chain(url)  # core chain only
    _billing_to(url, "0008_billing")
    customer = str(uuid.uuid4())
    run_sql(
        url,
        "INSERT INTO billing_customers (id, user_id) VALUES (%s, %s)",
        (customer, str(uuid.uuid4())),
    )
    run_sql(
        url,
        "INSERT INTO billing_credit_balances (customer_id, amount) VALUES (%s, -5)",
        (customer,),
    )
    # Restoring the CHECK would hide owed credits: the downgrade refuses.
    with pytest.raises(Exception, match="ck_billing_credit_balance_non_negative"):
        _billing_to(url, "-0007_billing")

    run_sql(url, "UPDATE billing_credit_balances SET amount = 0")
    _billing_to(url, "-0007_billing")
    assert not run_sql(
        url,
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'billing_credit_grants'",
    )


def test_issued_at_is_added_and_removed(
    scratch_database: str,
) -> None:
    """Migration 0009_billing (ADR-0003 open item 10)."""
    url = scratch_database
    upgrade_core_chain(url)
    _billing_to(url, "head")
    query = (
        "SELECT count(*) FROM information_schema.columns WHERE table_name = "
        "'billing_operation_intents' AND column_name = 'issued_at'"
    )
    assert run_sql(url, query)[0][0] == 1
    _billing_to(url, "-0008_billing")
    assert run_sql(url, query)[0][0] == 0
