"""t1_notes — tenant CRUD + event + paid gate (experiment A)."""

from _dx import (
    event_classes,
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
    text,
)

NOTES = module_dir("notes")


def test_fn_module_and_crud_routes() -> None:
    methods = {r.method for r in routes(NOTES)}
    assert {"GET", "POST", "DELETE"} <= methods and methods & {"PUT", "PATCH"}, methods


def test_fn_tests_written() -> None:
    assert module_tests(NOTES)


def test_fn_typed_event_published() -> None:
    """A producer Event class on a notes.*.v1 channel, published with .publish()."""
    channels = event_classes(NOTES).values()
    assert any(c.startswith("notes.") and c.endswith(".v1") for c in channels), channels
    assert ".publish(" in text(NOTES)


GATES = ("Subscribed(", "SubscriptionActive(", "Entitled(", "Credits(")


def test_fn_paid_gate_on_create() -> None:
    """A billing atom on the create route: inline, or through the module-level
    policy constants the route names, transitively (``PAID = Subscribed(...)``,
    ``GATE = MEMBER & PAID``, ``PAID_DEP = Depends(require(PAID))``). A
    ``require(...)`` alias without a billing atom in it is not a paid gate."""
    posts = [r for r in routes(NOTES) if r.method == "POST"]
    assert any(
        any(g in r.text or g in expanded_head(r, NOTES) for g in GATES) for r in posts
    ), "no billing gate on note creation"


def test_dev_writes_keep_the_route_defaults() -> None:
    """Model E: writes stay authenticated with automatic retry dedupe (no
    public() / no_idempotency() on a tenant CRUD route)."""
    weakened = [
        f"{r.method} {r.path} ({r.function})"
        for r in mutating_routes(NOTES)
        if not keeps_route_defaults(r)
    ]
    assert not weakened, weakened


def test_dev_notes_tables_are_tenant_scoped() -> None:
    bad = [
        m.name
        for m in tenant_like_models(NOTES)
        if not is_tenant_scoped(m) and (m.table or "") not in exempt_tables()
    ]
    assert not bad, bad


def test_dev_rls_enabled_for_tenant_tables() -> None:
    mig = migrations_text(NOTES)
    tables = [m.table for m in models(NOTES) if is_tenant_scoped(m) and m.table]
    assert tables and all(
        f'"{t}"' in mig and "enable_tenant_rls" in mig for t in tables
    ), tables
