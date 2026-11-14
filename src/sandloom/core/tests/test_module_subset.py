"""Monolith ↔ microservices guarantee: any module subset boots standalone.

The deploy profiles in deploy/docker-compose.*.yml rely on SANDLOOM_MODULES
subsetting; these tests pin that behaviour at the loader level. A subset is
the named modules plus the default modules that provide what they REQUIRE
(``with_required_providers``), so a module that gains a required contract
needs no edit here. The subsets of the bundled modules (each boots alone,
the full set loads, their tasks load without Celery) are in
``tests/integration/with_batteries/test_module_subset_bundled_modules.py``;
the sixteen subsets of identity, tenancy, billing and admin, each booted in a
fresh interpreter (``create_app``, ``/health``, ``/ready``, ``sandloom
--help``) in the repository, from wheels on plain sandloom (P2) and through
the extras (P3) in clean venvs (I-19, A-B7, KB-04), are in
``tests/integration/module_subsets/``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.descriptors.module_names import installed_modules
from sandloom.core.drivers.bootstrap import build_infra_registry
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
    # The registry create_app binds: a module with tasks makes /ready open the
    # default taskqueue too (KB-02).
    core.set_infra(build_infra_registry(InMemorySchemaRegistry(), load_plugins=False))
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


def test_a_task_declares_and_runs_inline_without_celery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``@task`` imports no broker: with Celery absent the decorated function
    keeps its name and body, and a ``tasks.py`` still loads."""
    import sys

    from sandloom.sdk.tasks import periodic, task

    monkeypatch.setitem(sys.modules, "celery", None)

    @task(at_least_once=True)
    def add() -> int:
        """Add two numbers."""
        return 2 + 3

    @periodic(cron="0 3 * * *", id="probe.subset.bare")
    def bare() -> str:
        return "ran"

    assert add() == 5
    assert add.name == f"{__name__}.add"
    assert add.__doc__ == "Add two numbers."
    assert bare() == "ran"
    assert bare.id == "probe.subset.bare"
