"""The shipped chain must satisfy the strict profile, not only ORM scoping."""

from sandloom.sdk.testing import (  # sandloom: advanced migration chain helpers
    migrate_module,
    run_sql,
)

PACKAGE = "projecthub.modules.notifications"


def test_notifications_chain_enforces_tenant_rls(scratch_database: str) -> None:
    migrate_module(scratch_database, PACKAGE)
    flags = run_sql(
        scratch_database,
        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
        "WHERE relname='notifications_inapp'",
    )
    assert flags == [(True, True)]
    policies = run_sql(
        scratch_database,
        "SELECT policyname FROM pg_policies WHERE tablename='notifications_inapp'",
    )
    assert policies == [("notifications_inapp_tenant_isolation",)]

    migrate_module(scratch_database, PACKAGE, "0002_notifications", downgrade=True)
    assert run_sql(
        scratch_database,
        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
        "WHERE relname='notifications_inapp'",
    ) == [(False, False)]
    migrate_module(scratch_database, PACKAGE)
    assert run_sql(
        scratch_database,
        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
        "WHERE relname='notifications_inapp'",
    ) == [(True, True)]
