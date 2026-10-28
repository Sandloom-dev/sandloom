"""Module capability declarations + load-time preflight."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.capabilities import CapabilitySpec
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import CacheProvider, InfraProvider
from sandloom.core.infra.registry import (
    FeatureUnsupportedError,
    InfraRegistry,
    UnknownCapabilityError,
)
from sandloom.core.loader.plugin_loader import _preflight_capabilities


class _Cache(CacheProvider):
    def __init__(self, *, name, driver, options, supports):  # type: ignore[no-untyped-def]
        super().__init__(name=name, driver=driver, options=options)
        self._supports = frozenset(supports)

    def features(self) -> frozenset[str]:
        return self._supports

    async def get(self, key: str):  # type: ignore[no-untyped-def]
        return None

    async def set(self, key, value, *, ttl_seconds=None):  # type: ignore[no-untyped-def]
        return None

    async def add(self, key, value, *, ttl_seconds=None):  # type: ignore[no-untyped-def]
        return True

    async def delete(self, key):  # type: ignore[no-untyped-def]
        return None

    async def incr(self, key, *, ttl_seconds=None):  # type: ignore[no-untyped-def]
        return 1


def _core_with_cache(*, supports=("get", "set")) -> CoreFacade:  # type: ignore[no-untyped-def]
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="fake", options={}
                )
            }
        }
    )
    reg.register_capability(
        CapabilitySpec(
            name="cache",
            version="v1",
            provider_contract=CacheProvider,
            features=frozenset({"get", "set", "add", "delete", "incr"}),
        )
    )
    reg.register_driver(
        "cache",
        "fake",
        lambda cfg: _Cache(
            name=cfg.name, driver=cfg.driver, options={}, supports=supports
        ),
        # Build-free preflight validates against declared driver metadata.
        features=frozenset(supports),
    )
    core = CoreFacade(app=FastAPI())
    core.set_infra(reg)
    return core


def test_preflight_passes_for_satisfiable_requirement() -> None:
    core = _core_with_cache(supports=("get", "set", "incr"))
    desc = ModuleDescriptor(
        name="m",
        version="1.0",
        capabilities=[CapabilityRequirement("cache", features=frozenset({"incr"}))],
    )
    _preflight_capabilities(core, desc)  # no raise


def test_preflight_fails_for_unsupported_feature() -> None:
    core = _core_with_cache(supports=("get", "set"))
    desc = ModuleDescriptor(
        name="m",
        version="1.0",
        capabilities=[CapabilityRequirement("cache", features=frozenset({"incr"}))],
    )
    with pytest.raises(FeatureUnsupportedError):
        _preflight_capabilities(core, desc)


def test_preflight_unknown_capability_raises() -> None:
    core = _core_with_cache()
    desc = ModuleDescriptor(
        name="m",
        version="1.0",
        capabilities=[CapabilityRequirement("graph_store")],
    )
    with pytest.raises(UnknownCapabilityError):
        _preflight_capabilities(core, desc)


def test_optional_requirement_does_not_raise() -> None:
    core = _core_with_cache()
    desc = ModuleDescriptor(
        name="m",
        version="1.0",
        capabilities=[CapabilityRequirement("graph_store", required=False)],
    )
    _preflight_capabilities(core, desc)  # warns, no raise


def test_preflight_skipped_without_infra() -> None:
    core = CoreFacade(app=FastAPI())  # no set_infra
    desc = ModuleDescriptor(
        name="m",
        version="1.0",
        capabilities=[CapabilityRequirement("graph_store")],
    )
    _preflight_capabilities(core, desc)  # no infra → skipped


def test_database_instances_from_capability() -> None:
    desc = ModuleDescriptor(
        name="m",
        version="1.0",
        capabilities=[CapabilityRequirement("database", instance="analytics")],
    )
    assert desc.database_instances() == {"analytics"}
    assert desc.effective_capabilities() == desc.capabilities


def test_database_instances_defaults_to_default() -> None:
    desc = ModuleDescriptor(name="m", version="1.0")  # no database capability
    assert desc.database_instances() == {"default"}
    assert desc.effective_capabilities() == []


def test_unused_infra_provider_base_import() -> None:
    # Keep InfraProvider import meaningful (used by type-checkers / fakes).
    assert issubclass(_Cache, InfraProvider)
