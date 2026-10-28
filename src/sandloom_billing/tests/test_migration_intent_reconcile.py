"""Migration 0007_billing: intent reconciliation state and the checkout
session expiry (#72, ADR-0003 open item 13)."""

from sandloom.sdk.testing import run_sql, upgrade_core_chain
from sandloom_billing.tests.test_migration_subscription_origin import _billing_to

_NEW = {
    "psp_session_expires_at",
    "psp_expired_at",
    "reconcile_attempts",
    "reconcile_error",
}


def _intent_columns(url: str) -> set[str]:
    return {
        row[0]
        for row in run_sql(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'billing_operation_intents'",
        )
    }


def test_upgrade_adds_reconcile_state_and_downgrade_removes_it(
    scratch_database: str,
) -> None:
    url = scratch_database
    upgrade_core_chain(url)  # core chain only
    _billing_to(url, "head")
    assert _NEW <= _intent_columns(url)
    default = run_sql(
        url,
        "SELECT column_default FROM information_schema.columns WHERE table_name = "
        "'billing_operation_intents' AND column_name = 'reconcile_attempts'",
    )[0][0]
    assert str(default).startswith("0")

    _billing_to(url, "-0006_billing")
    assert not _NEW & _intent_columns(url)
