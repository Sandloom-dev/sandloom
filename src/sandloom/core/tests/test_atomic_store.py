"""The ``memory`` atomic_store driver against the kit.

The ``redis`` driver's own tests (the kit, the eviction check, the round trips,
the kernel's ``never_evicts_check=assume`` policy through the registry) live in
``packages/sandloom-redis/tests``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from sandloom.core.drivers.memory_atomic_store import MemoryAtomicStoreProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
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


def test_the_driverkit_helpers_are_the_ports_own() -> None:
    from sandloom.core.infra.ports import atomic_store as port
    from sandloom.driverkit import atomic_store as kit

    for name in kit.__all__:
        assert getattr(kit, name) is getattr(port, name), name
