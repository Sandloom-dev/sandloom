"""The ``sandloom.drivers`` entry points this distribution publishes.

The registry reads the wheel's entry points, never this source tree, so a typo
in ``pyproject.toml`` or a class that lost its ``tier`` fails here, in the job
that tests the built wheel, not when an application boots. No Redis is reached:
a driver is loaded and registered, never built.
"""

from __future__ import annotations

import logging
from importlib import metadata
from types import SimpleNamespace
from typing import Any

import pytest
from sandloom_redis.atomic_store import RedisAtomicStoreProvider
from sandloom_redis.broadcast import RedisBroadcastProvider
from sandloom_redis.cache import RedisCacheProvider
from sandloom_redis.eventbus.driver import StreamsEventBusProvider
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit import (
    AtomicStoreProvider,
    BroadcastProvider,
    CacheProvider,
    EventTransportProvider,
    LeaseProvider,
)

#: entry point name -> (the class it must load, the kit's provider base it extends)
_EXPECTED: dict[str, tuple[type, type]] = {
    "atomic_store.redis": (RedisAtomicStoreProvider, AtomicStoreProvider),
    "lease.redis": (RedisLeaseProvider, LeaseProvider),
    "broadcast.redis": (RedisBroadcastProvider, BroadcastProvider),
    "cache.redis": (RedisCacheProvider, CacheProvider),
    "eventbus.streams": (StreamsEventBusProvider, EventTransportProvider),
}


def _published() -> dict[str, metadata.EntryPoint]:
    return {
        entry.name: entry
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.dist is not None
        and entry.dist.name.replace("_", "-") == "sandloom-redis"
    }


def test_the_distribution_publishes_exactly_the_expected_drivers() -> None:
    assert sorted(_published()) == sorted(_EXPECTED)


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_every_entry_point_loads_its_class_and_the_kits_provider_base(
    name: str,
) -> None:
    expected, base = _EXPECTED[name]
    loaded = _published()[name].load()
    assert loaded is expected
    assert issubclass(loaded, base)


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_every_driver_declares_a_tier_production_accepts(name: str) -> None:
    """An undeclared tier is ``experimental``: production refuses it (B023)."""
    assert _published()[name].load().tier in {"beta", "stable"}


def _production(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True, accepts_risk=lambda risk: risk in risks, infra={}
    )


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_a_production_registry_accepts_every_driver_without_the_experimental_risk(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    capability, _, driver = name.partition(".")
    config = {
        capability: {
            "default": InstanceConfig(
                capability, "default", driver, {"url": "redis://127.0.0.1:1/0"}
            )
        }
    }
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra"):
        registry = build_infra_registry(
            InMemorySchemaRegistry(), settings=_production(), config=config
        )
    tier = registry.driver_tier(capability, driver)
    assert tier in {"beta", "stable"}
    assert not [r for r in caplog.records if "experimental" in r.getMessage()]
