"""CoreFacade ↔ InfraRegistry integration (capability accessors)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig, load_infra_config
from sandloom.core.infra.contracts import (
    CacheProvider,
    DatabaseProvider,
    ObjectStoreProvider,
    TaskQueueProvider,
)
from sandloom.core.infra.registry import InfraError
from sandloom.core.schema.registry import InMemorySchemaRegistry


@pytest.fixture()
def wired_core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    registry = build_infra_registry(
        InMemorySchemaRegistry(),
        config=load_infra_config(settings),
        load_plugins=False,
    )
    core.set_infra(registry)
    return core


def test_capability_accessors_return_correct_provider_types(
    wired_core: CoreFacade,
) -> None:
    assert isinstance(wired_core.database(), DatabaseProvider)
    assert isinstance(wired_core.cache(), CacheProvider)
    assert isinstance(wired_core.objectstore(), ObjectStoreProvider)
    assert isinstance(wired_core.taskqueue(), TaskQueueProvider)


def test_default_database_reuses_shared_engine(wired_core: CoreFacade) -> None:
    from sandloom.core.drivers.sqlalchemy_database import get_default_engine

    provider = wired_core.database("default")
    assert provider.engine is get_default_engine()  # type: ignore[attr-defined]
    # Shared engine is NOT owned by the registry (main disposes it).
    assert provider._owns_engine is False  # type: ignore[attr-defined]


def test_get_uow_factory_resolves_through_registry(wired_core: CoreFacade) -> None:
    factory = wired_core.get_uow_factory()
    assert factory is not None
    # Named instance that does not exist must raise, not silently fall back.
    with pytest.raises(InfraError):
        wired_core.get_uow_factory("nonexistent")


def test_broken_database_plugin_never_falls_back_to_another_database() -> None:
    class _BrokenRegistry:
        def resolve(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("configured database plugin failed")

    core = CoreFacade(app=FastAPI())
    core.set_infra(_BrokenRegistry())

    with pytest.raises(RuntimeError, match="configured database plugin failed"):
        core.get_uow_factory()


def test_bare_facade_has_no_uow_fallback() -> None:
    core = CoreFacade(app=FastAPI())

    with pytest.raises(RuntimeError, match="Infrastructure registry not configured"):
        core.get_uow_factory()
    with pytest.raises(RuntimeError, match="no longer supported"):
        core.register_uow_factory(lambda: object())


def test_named_database_resolution(wired_core: CoreFacade) -> None:
    wired_core.infra.configure_instance(
        InstanceConfig(
            capability="database",
            name="reporting",
            driver="sqlalchemy",
            options={"url": settings.database_url},
        )
    )
    provider = wired_core.database("reporting")
    assert provider.name == "reporting"
    # Distinct engine from the default (independent pool).
    from sandloom.core.drivers.sqlalchemy_database import get_default_engine

    assert provider.engine is not get_default_engine()  # type: ignore[attr-defined]


def test_infra_not_wired_raises() -> None:
    bare = CoreFacade(app=FastAPI())
    with pytest.raises(RuntimeError):
        _ = bare.infra
