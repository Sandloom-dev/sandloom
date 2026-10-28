"""Unit tests of the ``sandloom.core.testing`` helpers that need no database.

(``committing_uow`` / ``real_uow_client`` / ``ambient_uow`` /
``revoked_tokens`` run for real in the tenancy and billing suites.)
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from fastapi import APIRouter

from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.testing import (
    capture_effects,
    override_instance,
    registration_surface,
)


class _Provider:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _registry() -> InfraRegistry:
    registry = InfraRegistry()
    registry._config["cache"] = {  # test seam
        "default": InstanceConfig(
            capability="cache", name="default", driver="redis", options={"url": "a"}
        )
    }
    return registry


async def test_override_instance_restores_config_and_built_provider() -> None:
    registry = _registry()
    original = _Provider("a")
    registry._providers[("cache", "default")] = original
    core = types.SimpleNamespace(infra=registry)

    async with override_instance(core, "cache", driver="redis", url="b"):
        assert registry._config["cache"]["default"].options == {"url": "b"}
        assert ("cache", "default") not in registry._providers
        built = _Provider("b")
        registry._providers[("cache", "default")] = built

    assert built.closed and not original.closed
    assert registry._providers[("cache", "default")] is original
    assert registry._config["cache"]["default"].options == {"url": "a"}


async def test_override_instance_of_an_unconfigured_instance_is_removed() -> None:
    registry = _registry()
    core = types.SimpleNamespace(infra=registry)
    async with override_instance(core, "cache", "sessions", driver="redis", url="c"):
        assert "sessions" in registry._config["cache"]
    assert "sessions" not in registry._config["cache"]


async def test_capture_effects_records_exact_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = types.ModuleType("fake_service")
    service.publish_event = None  # type: ignore[attr-defined]
    service.record_audit = None  # type: ignore[attr-defined]
    captured = capture_effects(monkeypatch, service)

    await service.publish_event("x.created.v1", {"id": "1"}, database="default")  # type: ignore[attr-defined]
    service.record_audit("x.created", actor_id="u", required=True)  # type: ignore[attr-defined]

    assert captured == {
        "events": [("x.created.v1", {"id": "1"})],
        "audit": [("x.created", {"actor_id": "u", "required": True})],
    }


def test_registration_surface_records_without_registering() -> None:
    router = APIRouter(prefix="/w")

    @router.get("/ping")
    async def ping() -> dict[str, str]:  # pragma: no cover - never served
        return {}

    def contributor(*_: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    async def startup() -> None:  # pragma: no cover
        return None

    def register(core: Any) -> ModuleDescriptor:
        core.include_router(router)
        core.register_event_schema(channel="w.made.v1", name="w.made.v1", schema={})
        assert core.get_service("other.v1") is None  # reads are not recorded
        core.add_claims_contributor(contributor, critical=True)
        core.add_startup_task(startup, critical=True)
        core.logger.info("w registered")
        return ModuleDescriptor(name="widgets", version="0.1.0")

    surface = registration_surface(register)
    assert surface.descriptor.name == "widgets"
    assert surface.calls == [
        ("include_router", "router[GET /w/ping]"),
        (
            "register_event_schema",
            "channel='w.made.v1'",
            "name='w.made.v1'",
            surface.calls[1][3],
        ),
        ("add_claims_contributor", f"{__name__}.contributor", "critical=True"),
        ("add_startup_task", f"{__name__}.startup", "critical=True"),
    ]
    assert surface.calls[1][3].startswith("schema=mapping:")
    assert surface.of("add_startup_task") == [surface.calls[3]]
