"""Monolith ↔ microservices guarantee: any module subset boots standalone.

The deploy profiles in deploy/docker-compose.*.yml rely on SANDLOOM_MODULES
subsetting; these tests pin that behaviour at the loader level. A subset is
the named modules plus the default modules that provide what they REQUIRE
(``with_required_providers``), so a module that gains a required contract
needs no edit here.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.descriptors.module_names import installed_modules
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import load_modules_with_summary
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing.boot import module_topology, with_required_providers

#: The standard modules installed here (import paths).
DEFAULT_MODULES = [m.path for m in installed_modules()]


def _subset(*modules: str) -> list[str]:
    return with_required_providers(list(modules), DEFAULT_MODULES)


def _fresh_core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


def _route_paths(core: CoreFacade) -> set[str]:
    return {getattr(r, "path", "") for r in core.app.router.routes}


@pytest.mark.parametrize(
    "named,expected_prefix,absent_module,absent_prefix",
    [
        (["sandloom_tenancy"], "/api/v1/orgs", "sandloom_identity", "/auth"),
        (["sandloom_identity"], "/auth", "sandloom_tenancy", "/api/v1/orgs"),
        (
            ["sandloom_tenancy", "sandloom_billing"],
            "/api/v1/billing",
            "sandloom_identity",
            "/auth",
        ),
        (["sandloom_billing"], "/api/v1/billing", "sandloom_tenancy", "/api/v1/orgs"),
    ],
)
def test_module_subset_boots_alone(
    monkeypatch: pytest.MonkeyPatch,
    named: list[str],
    expected_prefix: str,
    absent_module: str,
    absent_prefix: str,
) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    modules = _subset(*named)
    monkeypatch.setattr(settings, "sandloom_modules", modules)

    core = _fresh_core()
    summary = load_modules_with_summary(core)

    assert summary.success(), summary.to_dict()
    assert {d.name for d in summary.loaded} == set(modules)

    paths = _route_paths(core)
    assert any(p.startswith(expected_prefix) for p in paths)
    if absent_module not in modules:
        assert not any(p.startswith(absent_prefix) for p in paths)


def test_full_monolith_set_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandloom_modules", list(DEFAULT_MODULES))
    monkeypatch.setattr(settings, "module_load_entrypoints", False)

    core = _fresh_core()
    summary = load_modules_with_summary(core)
    assert summary.success(), summary.to_dict()
    assert len(summary.loaded) == len(DEFAULT_MODULES)

    paths = _route_paths(core)
    for prefix in ("/auth", "/api/v1/orgs", "/api/v1/billing"):
        assert any(p.startswith(prefix) for p in paths), f"missing {prefix} routes"


def test_blacklist_excludes_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    # A module no other default module requires (billing, unless that changed).
    required = {
        provider
        for module in DEFAULT_MODULES
        for provider in _subset(module)
        if provider != module
    }
    excluded = next(
        m for m in ("sandloom_billing", *reversed(DEFAULT_MODULES)) if m not in required
    )
    monkeypatch.setattr(settings, "sandloom_modules", list(DEFAULT_MODULES))
    monkeypatch.setattr(settings, "module_blacklist", [excluded])

    core = _fresh_core()
    summary = load_modules_with_summary(core)
    loaded = {d.name for d in summary.loaded}
    assert excluded not in loaded
    assert set(DEFAULT_MODULES) - {excluded} <= loaded


def test_a_subset_includes_the_providers_of_required_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    for module in DEFAULT_MODULES:
        subset = _subset(module)
        provided = {c for m in subset for c in module_topology(m).provides}
        for member in subset:
            assert module_topology(member).requires <= provided, (module, subset)


async def _ready_status(core: CoreFacade, monkeypatch: pytest.MonkeyPatch) -> int:
    """/ready of a subset process with healthy (stubbed) local infra."""
    import httpx

    from sandloom.core.app_factory import _install_ops_endpoints
    from sandloom.core.descriptors import HealthResult

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.health_checks.clear()
    core.register_health_check("db", ok)
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "service_bindings", {})
    _install_ops_endpoints(core.app, core)
    transport = httpx.ASGITransport(app=core.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return (await client.get("/ready")).status_code


@pytest.mark.parametrize("module", DEFAULT_MODULES)
async def test_each_single_module_subset_is_ready(
    monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    modules = _subset(module)
    monkeypatch.setattr(settings, "sandloom_modules", modules)
    monkeypatch.setattr(settings, "module_required", list(modules))
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    core = _fresh_core()
    load_modules_with_summary(core)
    assert await _ready_status(core, monkeypatch) == 200


async def test_subset_whose_only_module_failed_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SANDLOOM_MODULES='["sandloom.biling"]'`` (typo) with the module optional: the
    process boots but serves nothing — it must never report ready."""
    monkeypatch.setattr(settings, "sandloom_modules", ["sandloom.biling"])
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    core = _fresh_core()
    summary = load_modules_with_summary(core)
    assert [f.name for f in summary.failed] == ["sandloom.biling"]
    core.app.state.module_summary = summary
    assert await _ready_status(core, monkeypatch) == 503
