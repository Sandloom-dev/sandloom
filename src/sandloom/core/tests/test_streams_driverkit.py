"""The ``streams`` driver reaches the kernel through the driver kit only.

The key namespace is a driver option (``prefix``) the kernel fills from
``EVENT_STREAM_PREFIX``; the transport reads no setting. The block window the
driver sizes its client timeout over is the kernel bus's own constant. Every
``safe_inc`` counter name of the driver resolves on the kernel metrics module.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sandloom_redis.cache as redis_cache
from sandloom_redis.eventbus import streams_transport
from sandloom_redis.eventbus.driver import (
    EventBusOptions,
    StreamsEventBusProvider,
    _LazyRedisClient,
    _streams_read_timeout,
    make_streams_driver,
)

from sandloom.core.config.settings import settings
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InstanceConfig,
    load_infra_config,
)
from sandloom.core.observability import metrics as kernel_metrics
from sandloom.core.testing.infra import override_instance
from sandloom.driverkit import _builtin_streams, eventbus_support
from sandloom.driverkit._builtin_streams import BUS_BLOCK_MS


def test_the_kernel_fills_the_prefix_option_from_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "event_bus_backend", "streams")
    monkeypatch.setattr(settings, "event_stream_prefix", "staging:")
    options = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE].options
    assert options["prefix"] == "staging:"
    provider = make_streams_driver()(
        InstanceConfig(
            capability="eventbus",
            name=DEFAULT_INSTANCE,
            driver="streams",
            options=dict(options),
        )
    )
    assert isinstance(provider, StreamsEventBusProvider)
    assert provider.prefix == "staging:"
    assert provider.key("orders.created") == "staging:orders.created"


async def test_the_entry_point_build_is_the_kernel_build() -> None:
    """``from_config`` (what the ``eventbus.streams`` entry point resolves to) gives
    the provider its Redis client factory: without one the transport cannot do
    I/O. A named instance with a ``url`` owns a client it closes."""
    provider = StreamsEventBusProvider.from_config(
        InstanceConfig(
            capability="eventbus",
            name="named",
            driver="streams",
            options={"url": "redis://127.0.0.1:6399/0", "prefix": "p:"},
        )
    )
    assert isinstance(provider, StreamsEventBusProvider)
    assert provider.prefix == "p:"
    assert provider._client_factory is not None
    assert provider._owned_client is not None
    await provider.aclose()


def test_a_prefix_left_out_is_no_namespace_and_reads_no_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "event_stream_prefix", "from-the-setting:")
    assert EventBusOptions().prefix == ""
    provider = StreamsEventBusProvider(
        name="default", driver="streams", options={"url": "redis://127.0.0.1:6399/0"}
    )
    assert provider.prefix == ""


def test_the_read_timeout_is_sized_over_the_kernel_bus_block_window() -> None:
    assert StreamsEventBus._BLOCK_MS == BUS_BLOCK_MS
    assert _streams_read_timeout() > BUS_BLOCK_MS / 1000


def test_a_named_instance_builds_its_client_through_the_cache_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver calls ``redis_cache`` by attribute at each call, so a test that
    patches that module is honoured."""
    seen: list[Any] = []

    def _build(url: str, **kwargs: Any) -> Any:
        seen.append((url, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(redis_cache, "build_transport_client", _build)
    lazy = _LazyRedisClient(
        EventBusOptions(url="redis://h:1/0"),
        read_timeout=12.0,
    )
    client = asyncio.run(lazy())
    assert client is not None
    assert seen == [
        (
            "redis://h:1/0",
            {
                "read_timeout": 12.0,
                "socket_connect_timeout_seconds": 0.5,
                "health_check_interval_seconds": 30,
            },
        )
    ]


_STREAMS_DIR = Path(streams_transport.__file__).parent


def _safe_inc_calls() -> list[tuple[str, set[str]]]:
    """``(counter name, label keys)`` of every ``safe_inc("<name>", {...})`` call
    in the built-in streams driver's package."""
    found: list[tuple[str, set[str]]] = []
    for path in sorted(_STREAMS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "safe_inc"
            ):
                name = node.args[0]
                assert isinstance(name, ast.Constant), f"{path.name}: literal name"
                labels = node.args[1] if len(node.args) > 1 else None
                keys = (
                    {str(k.value) for k in labels.keys if isinstance(k, ast.Constant)}
                    if isinstance(labels, ast.Dict)
                    else set()
                )
                found.append((str(name.value), keys))
    return found


def test_every_safe_inc_name_of_the_streams_driver_is_a_kernel_counter() -> None:
    """``safe_inc`` looks a counter up by name on the kernel metrics module and
    swallows a miss, so a renamed or mistyped counter would count nothing."""
    calls = _safe_inc_calls()
    assert len(calls) >= 7, "the scan found the driver's safe_inc calls"
    for name, label_keys in calls:
        counter = getattr(kernel_metrics, name, None)
        assert counter is not None, f"safe_inc({name!r}): no such kernel counter"
        assert set(counter._labelnames) == label_keys, name


def test_the_builtin_only_constants_are_not_public_driverkit_api() -> None:
    private = {
        "BUS_BLOCK_MS",
        "INVENTORY_STALE_SECONDS",
        "POISON_SCAN_COUNT",
        "RECLAIM_INTERVAL_SECONDS",
        "TARGETED_REPLAY_SKIPPED",
    }
    assert private.isdisjoint(eventbus_support.__all__)
    assert set(_builtin_streams.__all__) == private
    assert StreamsEventBus._BLOCK_MS == _builtin_streams.BUS_BLOCK_MS


class _Registry:
    """The slice of ``InfraRegistry`` ``override_instance`` calls."""

    def __init__(self) -> None:
        self.configured: list[InstanceConfig] = []

    def snapshot_instance(self, capability: str, name: str) -> object:
        return object()

    def configure_instance(self, cfg: InstanceConfig) -> None:
        self.configured.append(cfg)

    def restore_instance(self, snapshot: object) -> None:
        return None


async def _overridden_options(driver: str, **options: Any) -> dict[str, Any]:
    registry = _Registry()
    async with override_instance(
        SimpleNamespace(infra=registry), "eventbus", driver=driver, **options
    ):
        pass
    return dict(registry.configured[0].options)


def test_a_streams_override_stays_namespaced_like_the_kernel_filled_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "event_stream_prefix", "staging:")
    assert asyncio.run(_overridden_options("streams", url="redis://h:1/0")) == {
        "url": "redis://h:1/0",
        "prefix": "staging:",
    }
    # An explicit prefix wins; a driver without the option is left alone.
    explicit = asyncio.run(_overridden_options("streams", prefix="mine:"))
    assert explicit == {"prefix": "mine:"}
    assert asyncio.run(_overridden_options("memory")) == {}
