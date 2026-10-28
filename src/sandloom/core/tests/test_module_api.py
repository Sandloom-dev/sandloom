"""ModuleAPI: the narrow registration surface delegates, and hides internals."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleAPI


def _api() -> tuple[ModuleAPI, CoreFacade]:
    core = CoreFacade(app=FastAPI())
    return ModuleAPI(core), core


def test_include_router_delegates_to_app() -> None:
    api, core = _api()
    router = APIRouter()

    @router.get("/mod-thing")
    def _handler() -> dict[str, bool]:  # pragma: no cover - not called
        return {"ok": True}

    api.include_router(router)
    assert any("/mod-thing" in getattr(r, "path", "") for r in core.app.router.routes)


def test_service_register_and_get_delegate() -> None:
    api, core = _api()
    sentinel = object()
    api.register_service("demo.svc.v1", sentinel)
    assert api.get_service("demo.svc.v1").contract_impl is sentinel
    assert core.get_service("demo.svc.v1").contract_impl is sentinel
    assert core.service_registry.get("demo.svc.v1") is sentinel


def test_lifecycle_and_health_delegate() -> None:
    api, core = _api()

    async def _startup() -> None:  # pragma: no cover - not run here
        return None

    api.add_startup_task(_startup, critical=True)
    assert _startup in core.startup_tasks
    assert core.is_critical_startup_task(_startup)


def test_module_api_hides_kernel_internals() -> None:
    api, _core = _api()
    # The whole point: a module cannot reach the raw app, infra registry,
    # event bus, or UoW factory through the surface it is handed.
    for hidden in ("app", "infra", "event_bus", "get_uow_factory", "set_infra"):
        assert not hasattr(api, hidden), f"ModuleAPI must not expose {hidden!r}"
