"""t5_billing_contacts — tenant-owned table in an existing module.

Framework mode (a kernel-contributor task): the bundled ``billing``. App mode:
the seeded app's own ``workspace`` module (``seeds/teamhub``), the same
mechanism: a new ``TenantScoped`` table with RLS, in a new revision of the
module's own migration chain."""

import pytest
from _dx import (
    app_mode,
    changed_under,
    expanded_head,
    is_tenant_scoped,
    keeps_route_defaults,
    migrations_text,
    models,
    module_dir,
    module_prefix,
    mutating_routes,
    pick,
)

NAME = pick(framework="billing", app="workspace")
MODULE = module_dir(NAME)
ADMIN_GATES = ("require_org_role", "ROLE_ADMIN", 'OrgRole("admin"', "OrgRole('admin'")
#: The seeded module's applied baseline revision (seeds/teamhub).
SEED_BASELINE = "migrations/versions/0001_workspace_baseline.py"


def _contact_models() -> list:
    return [
        m for m in models(MODULE) if "contact" in (m.name + (m.table or "")).lower()
    ]


def _contact_writes() -> list:
    return [
        r for r in mutating_routes(MODULE) if "contact" in (r.path + r.function).lower()
    ]


def test_fn_model_and_routes() -> None:
    assert _contact_models()
    assert _contact_writes()


def test_fn_migration_in_billing_chain() -> None:  # name kept: results compare by it
    assert changed_under(module_prefix(NAME, "migrations/versions"))


def test_dev_tenant_scoped() -> None:
    assert all(is_tenant_scoped(m) for m in _contact_models())


def test_dev_rls_enabled() -> None:
    mig = migrations_text(MODULE)
    assert all(
        m.table and "enable_tenant_rls" in mig and m.table in mig
        for m in _contact_models()
    )


def test_dev_writes_keep_the_route_defaults() -> None:
    """Authenticated, with automatic retry dedupe (model E)."""
    assert all(keeps_route_defaults(r) for r in _contact_writes())


def test_dev_writes_admin_only() -> None:
    """App mode: "organization admins manage" — every contact write names the
    admin role, inline or through a policy constant."""
    if not app_mode():
        pytest.skip("app mode (the framework task text predates this check)")
    for r in _contact_writes():
        blob = r.text + "\n" + expanded_head(r, MODULE)
        assert any(g in blob for g in ADMIN_GATES), r.function


def test_dev_new_revision_not_baseline_edit() -> None:
    """App mode: the product runs in production, so the table comes in a NEW
    revision of the module's chain (``sandloom migrate new``); the applied
    baseline stays untouched (an edited applied revision never runs)."""
    if not app_mode():
        pytest.skip("app mode: the seed's applied baseline is known")
    versions = changed_under(module_prefix(NAME, "migrations/versions"))
    baseline = module_prefix(NAME) + SEED_BASELINE
    assert baseline not in versions, "the applied baseline revision was edited"
    assert [v for v in versions if v.endswith(".py") and v != baseline], versions
