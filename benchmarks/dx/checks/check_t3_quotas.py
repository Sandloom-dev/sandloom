"""t3_quotas — command contract + a call from another module (experiment C).

Framework mode: the caller is the bundled ``tenancy`` (its invitations). App
mode: bundled modules are installed packages, so the caller is the app's own
``invites`` module (the task text asks for it)."""

from _dx import (
    contract_callers,
    contracts_package,
    exempt_tables,
    is_tenant_scoped,
    module_dir,
    module_package,
    module_tests,
    pick,
    shared_contract_stubs,
    tenant_like_models,
    text,
)

QUOTAS = module_dir("quotas")
CALLER = module_dir(pick(framework="tenancy", app="invites"))


def test_fn_command_contract() -> None:
    """A @contract("quotas.*.v1") provider with a @command(idempotency_key=...)."""
    src = text(QUOTAS)
    assert '@contract("quotas.' in src or "@contract('quotas." in src
    assert "@command(" in src and "idempotency_key=" in src


def test_fn_generated_files() -> None:
    """sandloom generate wrote the module snapshot and the contract stub."""
    assert (QUOTAS / "module.snapshot.json").is_file()
    assert any("quotas" in p.name for p in shared_contract_stubs())


def test_fn_tenancy_calls_quotas() -> None:  # name kept: results compare by it
    """The caller declares the contract at module level from the generated stub."""
    src = text(CALLER)
    assert f"{contracts_package()}.quotas" in src
    assert any("QUOTAS" in c.upper() for c in contract_callers(CALLER))


def test_fn_tests_written() -> None:
    assert module_tests(QUOTAS)


def test_dev_quota_contract_is_required() -> None:
    """An absent quota service must not silently allow invitations: the gate is
    a required Contract(REF), never optional=True (spec 5.3)."""
    callers = [c for c in contract_callers(CALLER) if "QUOTAS" in c.upper()]
    assert callers and not any("optional=True" in c for c in callers), callers


def test_dev_quota_tables_tenant_scoped() -> None:
    """Per-org rows are TenantScoped or carry a reasoned exemption."""
    bad = [
        m.name
        for m in tenant_like_models(QUOTAS)
        if not is_tenant_scoped(m) and (m.table or "") not in exempt_tables()
    ]
    assert not bad, bad


def test_dev_no_cross_module_import() -> None:
    quotas = module_package("quotas")
    src = text(CALLER)
    assert f"from {quotas}" not in src and f"import {quotas}" not in src
