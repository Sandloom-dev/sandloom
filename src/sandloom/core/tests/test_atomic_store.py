"""The ``memory`` atomic_store driver against the kit, and the kernel's policy for
the ``redis`` driver of ``sandloom-redis`` (``never_evicts_check=assume``).

The ``redis`` driver's own tests (the kit, the eviction check, the round trips)
live in ``packages/sandloom-redis/tests``. The registry tests below build that
driver through the registry, as an application does, against the test Redis
(``SANDLOOM_TEST_CACHE_URL``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.drivers.memory_atomic_store import MemoryAtomicStoreProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import PermissionDenied
from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit.testing import AtomicStoreConformance

_VALUE_LIMIT = 8192


class _Clock:
    """A monotonic clock a test moves forward."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def _memory(**options: Any) -> MemoryAtomicStoreProvider:
    options.setdefault("max_value_bytes", _VALUE_LIMIT)
    provider = MemoryAtomicStoreProvider.from_config(
        InstanceConfig("atomic_store", "default", "memory", options)
    )
    provider.clock = _Clock()
    return provider


class TestMemoryAtomicStore(AtomicStoreConformance):
    value_limit_bytes = _VALUE_LIMIT

    # Checks that do not apply to the memory driver: it has no backend that
    # can fail, declares neither ``shared`` nor ``never_evicts``, and a
    # ``None`` attribute is not collected (a skip would be a test that runs on
    # no CI lane: tools/ci/skip_ledger.py).
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]
    test_harness_redacts_the_health_of_a_failing_backend = None  # type: ignore[assignment]
    test_shared_two_providers_of_one_store_see_each_other = None  # type: ignore[assignment]
    test_never_evicts_the_driver_refuses_an_evicting_backend = None  # type: ignore[assignment]
    test_never_evicts_the_refusal_holds_when_the_operator_vouches = None  # type: ignore[assignment]

    @pytest.fixture
    def make_provider(self) -> Callable[[], AtomicStoreProvider]:
        return _memory

    async def let_expire(self, provider: AtomicStoreProvider, ttl_seconds: int) -> None:
        clock = provider.clock  # type: ignore[attr-defined]
        assert isinstance(clock, _Clock)
        clock.offset += ttl_seconds + 1.1


async def test_the_memory_driver_declares_what_it_is() -> None:
    provider = _memory()
    assert provider.development_only is True
    assert provider.features() == {"never_evicts"}
    assert await provider.isolation_identity() == await _memory().isolation_identity()
    assert (await provider.isolation_identity() or "").startswith("memory:")


async def test_the_memory_driver_expires_a_key_in_real_time() -> None:
    provider = MemoryAtomicStoreProvider.from_config(
        InstanceConfig("atomic_store", "default", "memory", {})
    )
    await provider.set("k", "v", ttl_seconds=1)
    assert await provider.get("k") == "v"
    await asyncio.sleep(1.1)
    assert await provider.get("k") is None


# ------------------------------------------- the kernel's policy for redis
def _registry_store(*, is_prod: bool, risks: tuple[str, ...] = ()) -> Any:
    """The ``redis`` atomic_store the registry builds, ``never_evicts_check=assume``."""
    registry_settings: Any = SimpleNamespace(
        is_prod=is_prod, accepts_risk=lambda risk: risk in risks, infra={}
    )
    config = {
        "atomic_store": {
            "default": InstanceConfig(
                "atomic_store",
                "default",
                "redis",
                {
                    "url": settings.cache_url,
                    "max_value_bytes": _VALUE_LIMIT,
                    "never_evicts_check": "assume",
                },
            )
        }
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(),
        settings=registry_settings,
        config=config,
    )
    return registry.provider("atomic_store", "default")


@pytest.mark.parametrize(
    ("is_prod", "risks"),
    [(False, ()), (True, ("unverified_noeviction",))],
)
async def test_the_registry_opens_assume_in_dev_and_with_the_accepted_risk(
    is_prod: bool, risks: tuple[str, ...]
) -> None:
    provider = _registry_store(is_prod=is_prod, risks=risks)
    await provider.aopen()  # a real server: the policy is readable and noeviction
    await provider.aclose()


async def test_the_registry_refuses_assume_in_production_without_the_risk() -> None:
    provider = _registry_store(is_prod=True)
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await provider.aopen()


def test_the_driverkit_helpers_are_the_ports_own() -> None:
    from sandloom.core.infra.ports import atomic_store as port
    from sandloom.driverkit import atomic_store as kit

    for name in kit.__all__:
        assert getattr(kit, name) is getattr(port, name), name
