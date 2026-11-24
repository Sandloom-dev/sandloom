"""Tickets' tenant-scope migration installs RLS only where INFRA declares it.

Helpdesk's ``INFRA`` has no ``tenant_rls``: forced RLS there would hide every
ticket from a non-superuser role, so revision ``0005_tickets`` creates the
policy only under the runner's ``sandloom_tenant_rls`` switch.
"""

import pytest

from sandloom.sdk.infra import (  # sandloom: advanced INFRA override for the RLS switch
    settings,
)
from sandloom.sdk.testing import (  # sandloom: advanced migration chain helpers
    migrate_module,
    run_sql,
)

TABLE = "tickets_tickets"
MODULE = "helpdesk.modules.tickets"


def _rls(url: str) -> list[tuple[bool, bool]]:
    return run_sql(
        url,
        f"SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='{TABLE}'",
    )


def _infra(url: str, *, tenant_rls: bool) -> dict:
    return {
        "database": {
            "default": {"driver": "sqlalchemy", "tenant_rls": tenant_rls, "url": url}
        }
    }


def test_without_tenant_rls_no_policy_is_installed(scratch_database, monkeypatch):
    monkeypatch.setattr(settings, "infra", _infra(scratch_database, tenant_rls=False))
    migrate_module(scratch_database, MODULE)
    assert _rls(scratch_database) == [(False, False)]
    assert (
        run_sql(
            scratch_database,
            f"SELECT policyname FROM pg_policies WHERE tablename='{TABLE}'",
        )
        == []
    )


def test_with_tenant_rls_the_forced_policy_is_installed(scratch_database, monkeypatch):
    monkeypatch.setattr(settings, "infra", _infra(scratch_database, tenant_rls=True))
    migrate_module(scratch_database, MODULE)
    assert _rls(scratch_database) == [(True, True)]


@pytest.mark.parametrize("tenant_rls", [False, True])
def test_downgrade_walks_back_either_way(scratch_database, monkeypatch, tenant_rls):
    monkeypatch.setattr(
        settings, "infra", _infra(scratch_database, tenant_rls=tenant_rls)
    )
    migrate_module(scratch_database, MODULE)
    migrate_module(scratch_database, MODULE, "0004_tickets", downgrade=True)
    assert _rls(scratch_database) == [(False, False)]
