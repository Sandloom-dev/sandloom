"""t10_org_banner — admin-only write endpoint on a tenant row (t4-like, both modes)."""

from _dx import (
    audited_or_evented,
    exempt_tables,
    expanded_head,
    is_tenant_scoped,
    keeps_route_defaults,
    migrations_text,
    models,
    module_dir,
    module_tests,
    mutating_routes,
    routes,
    tenant_like_models,
)

BANNER = module_dir("banner")
ADMIN_GATES = ("require_org_role", "ROLE_ADMIN", 'OrgRole("admin"', "OrgRole('admin'")


def test_fn_read_and_write_routes() -> None:
    methods = {r.method for r in routes(BANNER)}
    assert "GET" in methods and mutating_routes(BANNER), methods


def test_fn_tests_written() -> None:
    assert module_tests(BANNER)


def test_dev_writes_admin_only() -> None:
    """Every write names the admin role: inline or through a policy constant."""
    for r in mutating_routes(BANNER):
        blob = r.text + "\n" + expanded_head(r, BANNER)
        assert any(g in blob for g in ADMIN_GATES), r.function


def test_dev_writes_keep_the_route_defaults() -> None:
    """Authenticated, with automatic retry dedupe (model E)."""
    weakened = [
        r.function for r in mutating_routes(BANNER) if not keeps_route_defaults(r)
    ]
    assert not weakened, weakened


def test_dev_banner_tables_are_tenant_scoped() -> None:
    bad = [
        m.name
        for m in tenant_like_models(BANNER)
        if not is_tenant_scoped(m) and (m.table or "") not in exempt_tables()
    ]
    assert not bad, bad


def test_dev_rls_enabled_for_tenant_tables() -> None:
    mig = migrations_text(BANNER)
    tables = [m.table for m in models(BANNER) if is_tenant_scoped(m) and m.table]
    assert tables and all(
        f'"{t}"' in mig and "enable_tenant_rls" in mig for t in tables
    ), tables


def test_dev_audited_or_evented() -> None:
    """Who changed it and when: an audit row or an event, explicit or the
    framework's automatic ``sandloom.sensitive_write`` row on every write (its
    resolved policy has a risk, ``"audit": "automatic"``)."""
    assert audited_or_evented(mutating_routes(BANNER), BANNER)
