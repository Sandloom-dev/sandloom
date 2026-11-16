"""The ``memory`` cache driver: the ``CacheConformance`` kit, the tier, and that
only an INFRA that names it ever selects it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

import sandloom.core.infra.config as config_module
from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import (
    BUILTIN_DRIVER_ROWS,
    build_infra_registry,
)
from sandloom.core.drivers.memory_cache import MemoryCacheProvider
from sandloom.core.infra.config import (
    InstanceConfig,
    load_infra_config,
    use_default_infra,
)
from sandloom.core.infra.errors import InvalidArgument, TooLarge
from sandloom.core.infra.ports.cache import CacheProvider
from sandloom.core.infra.registry import DevelopmentOnlyDriverRefused
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit.testing import CacheConformance


class _Clock:
    """A monotonic clock a test moves forward."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def _memory(**options: Any) -> MemoryCacheProvider:
    provider = MemoryCacheProvider.from_config(
        InstanceConfig("cache", "default", "memory", options)
    )
    provider.clock = _Clock()
    return provider


class TestMemoryCache(CacheConformance):
    # The driver has no backend that can fail, so the two harness checks of a
    # failing backend do not apply. A ``None`` attribute is not collected (a
    # skip would be a test that runs on no CI lane: tools/ci/skip_ledger.py).
    # The driver declares ``cas``, so the check of a driver without it is gone.
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]
    test_harness_redacts_the_health_of_a_failing_backend = None  # type: ignore[assignment]
    test_without_cas_both_calls_raise_unsupported = None  # type: ignore[assignment]

    @pytest.fixture
    def make_provider(self) -> Callable[[], CacheProvider]:
        return _memory

    async def let_expire(self, provider: CacheProvider, ttl_seconds: int) -> None:
        clock = provider.clock  # type: ignore[attr-defined]
        assert isinstance(clock, _Clock)
        clock.offset += ttl_seconds + 1.1


# ----------------------------------------------------------- what it declares
async def test_the_memory_driver_declares_what_it_is() -> None:
    provider = _memory()
    assert provider.contract_version == 2
    assert provider.development_only is True
    assert provider.features() == {"cas"}
    assert set(provider.performance_hints) == {
        "get_many",
        "set_many",
        "windowed_counter",
    }
    identity = await provider.isolation_identity() or ""
    assert identity.startswith("memory:")
    assert identity == await _memory().isolation_identity()


async def test_the_memory_driver_expires_a_key_in_real_time() -> None:
    provider = MemoryCacheProvider.from_config(
        InstanceConfig("cache", "default", "memory", {})
    )
    await provider.set("k", "v", ttl_seconds=1)
    assert await provider.get("k") == "v"
    time.sleep(1.1)
    assert await provider.get("k") is None


async def test_two_instances_do_not_share_keys() -> None:
    first, second = _memory(), _memory()
    await first.set("k", "v")
    assert await second.get("k") is None


async def test_arguments_outside_the_contract_are_refused() -> None:
    provider = _memory(max_value_bytes=8)
    with pytest.raises(InvalidArgument):
        await provider.get("k" * 1025)
    with pytest.raises(InvalidArgument):
        await provider.get("")
    with pytest.raises(InvalidArgument):
        await provider.set("k", "v", ttl_seconds=0)
    with pytest.raises(InvalidArgument):
        await provider.incr("k", ttl_seconds=-1)
    with pytest.raises(TooLarge):
        await provider.set("k", "too large a value")
    assert await provider.get("k") is None


async def test_the_harness_cache_speaks_the_contract() -> None:
    """``InMemoryCache`` (unit tests) declares only the spec feature, fails with
    the infra error a driver raises, and refuses a non-integer ``incr``."""
    from sandloom.core.infra.errors import Unavailable
    from sandloom.core.testing import InMemoryCache

    cache = InMemoryCache()
    assert cache.features() == {"cas"}
    await cache.set("text", "not a number")
    with pytest.raises(InvalidArgument):
        await cache.incr("text")
    assert await cache.incr("count") == 1
    down = InMemoryCache(broken=True)
    for call in (
        lambda: down.get("k"),
        lambda: down.set("k", "v"),
        lambda: down.incr("k"),
        lambda: down.get_many(["k"]),
    ):
        with pytest.raises(Unavailable):
            await call()


# ----------------------------------------------------- tier and selection
def _config(driver: str = "memory") -> dict[str, dict[str, InstanceConfig]]:
    return {"cache": {"default": InstanceConfig("cache", "default", driver, {})}}


def _prod(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True, accepts_risk=lambda risk: risk in risks, infra={}
    )


def _dev() -> Any:
    return SimpleNamespace(is_prod=False, accepts_risk=lambda risk: False, infra={})


def test_the_memory_cache_is_registered_with_no_pending_delivery() -> None:
    (row,) = [r for r in BUILTIN_DRIVER_ROWS if r[:2] == ("cache", "memory")]
    assert row[2].endswith("memory_cache:MemoryCacheProvider")
    assert row[5] == ""  # delivered_by: not a promise, the driver ships


@pytest.mark.parametrize("risks", [(), ("experimental_driver",)])
def test_production_refuses_the_memory_cache_whatever_the_risks(
    risks: tuple[str, ...],
) -> None:
    with pytest.raises(DevelopmentOnlyDriverRefused) as exc:
        build_infra_registry(
            InMemorySchemaRegistry(),
            settings=_prod(*risks),
            config=_config(),
            load_plugins=False,
        )
    assert exc.value.sandloom_code == "SANDLOOM-B022"


def test_outside_production_a_configured_memory_cache_is_served() -> None:
    registry = build_infra_registry(
        InMemorySchemaRegistry(),
        settings=_dev(),
        config=_config(),
        load_plugins=False,
    )
    provider = registry.provider("cache", "default")
    assert isinstance(provider, MemoryCacheProvider)


def test_no_default_names_the_memory_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing but an INFRA that says ``driver: memory`` selects it: the
    # synthesized default cache is the composition's selection (here ``redis``,
    # as the standard one), whatever the profile.
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    monkeypatch.setattr(settings, "infra", {}, raising=False)  # not .env.test's
    use_default_infra({"cache": {"default": {"driver": "redis"}}})
    assert load_infra_config(settings)["cache"]["default"].driver == "redis"


def test_an_infra_that_names_it_selects_it() -> None:
    chosen: Any = SimpleNamespace(
        **{
            **vars(settings),
            "infra": {"cache": {"default": {"driver": "memory"}}},
        }
    )
    config = load_infra_config(chosen)
    assert config["cache"]["default"].driver == "memory"
    registry = build_infra_registry(
        InMemorySchemaRegistry(),
        settings=_dev(),
        config=config,
        load_plugins=False,
    )
    assert isinstance(registry.provider("cache", "default"), MemoryCacheProvider)
