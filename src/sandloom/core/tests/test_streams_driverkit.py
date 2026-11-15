"""The ``streams`` driver reaches the kernel through the driver kit only.

The key namespace is a driver option (``prefix``) the kernel fills from
``EVENT_STREAM_PREFIX``; the transport reads no setting. The block window the
driver sizes its client timeout over is the kernel bus's own constant, and the
shared Redis client helpers of the driver kit call the cache driver at each call.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers import redis_cache
from sandloom.core.drivers.event_bus_driver import (
    EventBusOptions,
    StreamsEventBusProvider,
    _streams_read_timeout,
    make_streams_driver,
)
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InstanceConfig,
    load_infra_config,
)
from sandloom.driverkit import redis_client
from sandloom.driverkit.eventbus_support import BUS_BLOCK_MS


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


def test_the_redis_client_helpers_call_the_cache_driver_at_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def _build(url: str, **kwargs: Any) -> Any:
        seen.append((url, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(redis_cache, "build_transport_client", _build)
    client = redis_client.build_transport_client("redis://h:1/0", read_timeout=12.0)
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
