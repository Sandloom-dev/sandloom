"""Lifecycle/loader reliability: critical startup tasks, registration rollback."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import ModuleDescriptor, ModuleLoadSummary
from sandloom.core.facade import CoreFacade
from sandloom.core.lifecycle import CriticalStartupError, run_startup
from sandloom.core.loader.plugin_loader import (
    ModuleRegistrationError,
    _invoke_register,
    _load_entry_points,
    _load_list,
)
from sandloom.core.module_api import ModuleAPI
from sandloom.core.schema.registry import InMemorySchemaRegistry


@pytest.mark.asyncio
async def test_critical_startup_task_failure_aborts_startup() -> None:
    core = CoreFacade(app=FastAPI())

    async def boom() -> None:
        raise RuntimeError("infra down")

    core.add_startup_task(boom, critical=True)
    with pytest.raises(CriticalStartupError):
        await run_startup(core)


@pytest.mark.asyncio
async def test_non_critical_startup_task_failure_is_tolerated() -> None:
    core = CoreFacade(app=FastAPI())
    ran = {"after": False}

    async def boom() -> None:
        raise RuntimeError("optional thing failed")

    async def after() -> None:
        ran["after"] = True

    core.add_startup_task(boom)
    core.add_startup_task(after)
    await run_startup(core)  # no raise
    assert ran["after"] is True


def test_module_without_register_is_an_error() -> None:
    class FakeModule:
        __name__ = "sandloom.fake"

    core = CoreFacade(app=FastAPI())
    with pytest.raises(ModuleRegistrationError):
        _invoke_register(FakeModule(), ModuleAPI(core))


def test_failed_register_commits_nothing() -> None:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())

    # A register() that buffers writes then fails midway must commit nothing —
    # including the event schema (the old snapshot path left orphan schemas).
    with pytest.raises(RuntimeError):
        with core.module_registration("broken") as api:
            api.register_service("broken.thing.v1", object())
            api.add_startup_task(lambda: None, critical=True)
            api.register_event_schema("broken.thing.v1", "BrokenThing", None)
            router = APIRouter()

            @router.get("/broken")
            def broken() -> dict[str, bool]:  # pragma: no cover
                return {"ok": True}

            api.include_router(router)
            raise RuntimeError("register failed midway")

    assert core.service_registry.try_get("broken.thing.v1") is None
    assert core.startup_tasks == []
    assert core.get_schema_registry().get("broken.thing.v1") is None
    assert all("/broken" not in getattr(r, "path", "") for r in core.app.router.routes)


def test_successful_register_commits_atomically() -> None:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())

    with core.module_registration("ok") as api:
        api.register_service("ok.thing.v1", object())
        router = APIRouter()

        @router.get("/ok-route")
        def ok_route() -> dict[str, bool]:  # pragma: no cover
            return {"ok": True}

        api.include_router(router)
        # Buffered, not yet applied: the kernel is untouched until commit.
        assert core.service_registry.try_get("ok.thing.v1") is None

    assert core.service_registry.try_get("ok.thing.v1") is not None
    assert any("/ok-route" in getattr(r, "path", "") for r in core.app.router.routes)


def test_loader_records_no_register_module_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    class NoRegister:
        __name__ = "sandloom.fake_no_register"

    monkeypatch.setattr(importlib, "import_module", lambda name: NoRegister())
    core = CoreFacade(app=FastAPI())
    summary = ModuleLoadSummary(explicit=["sandloom.fake_no_register"])
    _load_list(["sandloom.fake_no_register"], core, summary)
    assert [f.name for f in summary.failed] == ["sandloom.fake_no_register"]
    assert summary.loaded == []


def test_entrypoint_fail_fast_honors_module_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry point that fails at ep.load() (desc is None) must still trip
    fail-fast when it is listed in module_required — mirroring _load_list.
    """
    from sandloom.core.loader import plugin_loader

    class _FailingEntryPoint:
        name = "sandloom.required_ep"

        def load(self) -> object:  # test stub
            raise RuntimeError("import boom")

    class _EntryPoints:
        def select(self, group: str) -> list[object]:
            return [_FailingEntryPoint()] if group == "sandloom.modules" else []

    monkeypatch.setattr(plugin_loader.metadata, "entry_points", lambda: _EntryPoints())
    monkeypatch.setattr(settings, "module_fail_fast", True, raising=False)
    monkeypatch.setattr(
        settings, "module_required", ["sandloom.required_ep"], raising=False
    )

    core = CoreFacade(app=FastAPI())
    summary = ModuleLoadSummary(explicit=[])
    with pytest.raises(RuntimeError, match="import boom"):
        _load_entry_points(core, summary)


def test_event_bus_instance_is_owned_by_facade() -> None:
    core_a = CoreFacade(app=FastAPI())
    core_b = CoreFacade(app=FastAPI())
    bus_a, bus_b = object(), object()
    core_a.set_event_bus(bus_a)
    core_b.set_event_bus(bus_b)
    assert core_a.event_bus is bus_a
    assert core_b.event_bus is bus_b


def test_loaded_descriptor_routes_still_included() -> None:
    """Sanity: rollback machinery must not break the happy path."""
    router = APIRouter()

    @router.get("/mod-ok")
    def ok() -> dict[str, bool]:  # pragma: no cover
        return {"ok": True}

    class GoodModule:
        __name__ = "sandloom.good"

        @staticmethod
        def register(core: CoreFacade) -> ModuleDescriptor:
            return ModuleDescriptor(
                name="sandloom.good", version="1.0.0", routes=router
            )

    import importlib

    core = CoreFacade(app=FastAPI())
    summary = ModuleLoadSummary(explicit=["sandloom.good"])
    orig_import = importlib.import_module
    importlib.import_module = lambda name: GoodModule()  # type: ignore[assignment]
    try:
        _load_list(["sandloom.good"], core, summary)
    finally:
        importlib.import_module = orig_import  # type: ignore[assignment]
    assert [d.name for d in summary.loaded] == ["sandloom.good"]
    assert any("/mod-ok" in getattr(r, "path", "") for r in core.app.router.routes)
