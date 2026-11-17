"""``InfraProvider.aclose_shared`` / ``InfraRegistry.aclose_shared``: what a
driver keeps for the whole process (a default client) is closed at shutdown by
the registry, which names no driver and imports none.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi import FastAPI

from sandloom.core import app_factory
from sandloom.core.drivers.memory_cache import MemoryCacheProvider
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.lifecycle import run_shutdown
from sandloom.core.schema.registry import InMemorySchemaRegistry


class _Shared(MemoryCacheProvider):
    """A driver with a process-wide resource, recording who closed it."""

    closed: ClassVar[list[str]] = []
    label: ClassVar[str] = "shared"

    @classmethod
    async def aclose_shared(cls) -> None:
        cls.closed.append(cls.label)


class _Other(_Shared):
    label = "other"


class _Failing(_Shared):
    label = "failing"

    @classmethod
    async def aclose_shared(cls) -> None:
        cls.closed.append(cls.label)
        raise RuntimeError("cannot close")


@pytest.fixture(autouse=True)
def _fresh() -> None:
    _Shared.closed = []


async def test_the_base_answer_closes_nothing() -> None:
    assert await MemoryCacheProvider.aclose_shared() is None


async def test_the_registry_closes_what_each_loaded_driver_shares_in_a_stable_order() -> (
    None
):
    registry = InfraRegistry()
    registry.register_driver("cache", "zeta", _Other)
    registry.register_driver("cache", "alpha", _Shared)
    await registry.aclose_shared()
    assert _Shared.closed == ["other", "shared"]  # by class name: _Other, _Shared


async def test_a_driver_that_fails_to_close_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = InfraRegistry()
    registry.register_driver("cache", "bad", _Failing)
    registry.register_driver("cache", "good", _Shared)
    with caplog.at_level(logging.WARNING):
        await registry.aclose_shared()  # never raises
    assert sorted(_Shared.closed) == ["failing", "shared"]
    assert any("_Failing" in record.getMessage() for record in caplog.records)


async def test_the_shutdown_of_an_app_closes_what_the_drivers_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel's shutdown reaches the hook: it names no driver module."""
    registry = InfraRegistry()
    registry.register_driver("cache", "shared", _Shared)
    monkeypatch.setattr(
        app_factory.lifecycle,
        "settings",
        SimpleNamespace(
            is_prod=False, schema_registry_path=None, production_profile="standard"
        ),
    )
    core = CoreFacade(app=FastAPI())
    app_factory._register_infra_lifecycle(core, InMemorySchemaRegistry(), registry)
    await run_shutdown(core)
    assert _Shared.closed == ["shared"]


async def test_a_driver_registered_lazily_and_never_loaded_is_not_imported() -> None:
    """An unselected driver has nothing to close: asking it would import it."""
    registry = InfraRegistry()
    registry.register_driver("cache", "lazy", "sandloom_does_not_exist.cache:Missing")
    await registry.aclose_shared()  # imports nothing, so it cannot fail
    assert _Shared.closed == []
