"""Migration 0006_billing: one subscription per checkout intent (ADR-0003
open item 16)."""

import uuid

import pytest

from sandloom.sdk.testing import run_sql, upgrade_core_chain
from sandloom_billing.tests.test_migration_subscription_origin import _billing_to


def test_one_subscription_per_intent_index(
    scratch_database: str,
) -> None:
    url = scratch_database
    upgrade_core_chain(url)  # core chain only
    _billing_to(url, "0006_billing")

    customer, intent = str(uuid.uuid4()), str(uuid.uuid4())
    run_sql(
        url,
        "INSERT INTO billing_customers (id, user_id) VALUES (%s, %s)",
        (customer, str(uuid.uuid4())),
    )
    insert = (
        "INSERT INTO billing_subscriptions (customer_id, stripe_subscription_id, "
        "status, cancel_at_period_end, intent_id) VALUES (%s, %s, 'active', false, %s)"
    )
    run_sql(url, insert, (customer, "sub_one", intent))
    with pytest.raises(Exception, match="uq_billing_subscriptions_intent_id"):
        run_sql(url, insert, (customer, "sub_two", intent))
    # Intent-less rows (NULL) are not constrained.
    run_sql(url, insert, (customer, "sub_three", None))
    run_sql(url, insert, (customer, "sub_four", None))

    _billing_to(url, "-0005_billing")
    columns = {
        row[0]
        for row in run_sql(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'billing_subscriptions'",
        )
    }
    assert "intent_id" not in columns
