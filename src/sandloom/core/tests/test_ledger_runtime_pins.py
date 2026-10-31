"""Runtime pins that close gaps found by the guarantee ledger (``guarantees.yml``).

Each test pins one documented promise that had no enforcing test; the ledger
entry it closes is named in the docstring. No database or Redis needed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from sandloom.core.config.accepted_risks import RISK_IDS
from sandloom.core.infra import access, active
from sandloom.core.orm import TenantScoped
from sandloom.core.tenant import tenant_filter
from sandloom.core.tenant_context import MissingTenantError, bind_tenant


@pytest.mark.parametrize("surface", ["cache", "notifier"])
def test_capability_surfaces_raise_without_a_bound_registry(
    surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-INFRA-02: with no registry bound, ``cache()`` / ``notifier()`` raise (with
    the composition-root hint) in every environment — no implicit default."""
    monkeypatch.setattr(active, "_active_infra", None)
    with pytest.raises(RuntimeError, match="Infrastructure registry not bound"):
        getattr(access, surface)()


def test_tenant_filter_fails_closed_and_binds_the_current_tenant() -> None:
    """G-TENANT-07: ``tenant_filter`` raises with no tenant bound, else filters on it."""
    model = SimpleNamespace(tenant_id=sa.column("tenant_id"))
    with pytest.raises(MissingTenantError):
        tenant_filter(model)
    tenant = uuid.uuid4()
    with bind_tenant(tenant):
        clause = tenant_filter(model)
    assert clause.right.value == tenant
    assert str(clause.compile()) == "tenant_id = :tenant_id_1"


def test_tenant_scoped_column_is_indexed_and_required() -> None:
    """C-TENANT-01: ``TenantScoped`` declares an indexed, NOT NULL ``tenant_id``."""
    column = TenantScoped.__dict__["tenant_id"].column
    assert column.index is True
    assert column.nullable is False


def test_accepted_risk_ids_are_pinned() -> None:
    """G-RISK-03: the accepted-risk ids are stable — adding or dropping one is a
    deliberate change of this list, ADR-0006 and the ledger."""
    assert sorted(RISK_IDS) == [
        "command_dedupe_below_retry_horizon",
        "experimental_capability",
        "fake_psp_in_prod",
        "local_storage_in_prod",
        "log_notifier_in_prod",
        "relay_disabled",
        "saga_horizon_beyond_command_dedupe",
        "shared_hs_secret_split",
        "unversioned_ddl_repair",
        "volatile_event_bus",
        "weak_password_hash",
    ]
