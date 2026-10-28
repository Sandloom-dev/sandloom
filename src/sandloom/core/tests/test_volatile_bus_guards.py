"""Durable consumers must not silently run on a volatile bus (M7)."""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.consumers.callbacks import (
    VolatileBusError,
    subscribe_to_bus,
)
from sandloom.core.drivers.event_bus_driver import make_event_bus_drivers
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry


class _PubSubLikeBus:
    """Cross-process, volatile, no consumer groups (like Redis Pub/Sub)."""

    durable = False
    in_process = False

    def __init__(self) -> None:
        self.subscribed: list[str] = []

    async def subscribe(self, channel: str, callback: Any) -> str:
        self.subscribed.append(channel)
        return "handle"


async def _noop(_env: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_durable_consumer_refused_on_pubsub_bus() -> None:
    bus = RedisEventBus(InMemorySchemaRegistry(), client_factory=None)
    with pytest.raises(VolatileBusError):
        await subscribe_to_bus(bus, "core.thing.v1", _noop, "notifier@thing")
    assert bus._subs == {}  # nothing half-registered


@pytest.mark.asyncio
async def test_volatile_opt_in_outside_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["volatile_event_bus"])
    bus = _PubSubLikeBus()
    assert await subscribe_to_bus(bus, "core.thing.v1", _noop, "g") == "handle"


@pytest.mark.asyncio
async def test_memory_bus_allowed_in_dev_refused_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = InMemoryEventBus(InMemorySchemaRegistry())
    await subscribe_to_bus(bus, "core.thing.v1", _noop, "g")  # dev/test: fine

    monkeypatch.setattr(settings, "environment", Environment.prod)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["volatile_event_bus"])
    # the risk admits a volatile DRIVER in prod, never durable consumers on it
    with pytest.raises(VolatileBusError):
        await subscribe_to_bus(bus, "core.other.v1", _noop, "g2")


def test_driver_refuses_volatile_bus_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    drivers = make_event_bus_drivers(InMemorySchemaRegistry())
    monkeypatch.setattr(settings, "environment", Environment.prod)
    for driver in ("memory", "redis"):
        cfg = InstanceConfig(capability="eventbus", name="side", driver=driver)
        with pytest.raises(RuntimeError, match="volatile"):
            drivers[driver](cfg)
    # The accepted risk admits the volatile driver.
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["volatile_event_bus"])
    for driver in ("memory", "redis"):
        cfg = InstanceConfig(capability="eventbus", name="side", driver=driver)
        assert drivers[driver](cfg).bus() is not None
    streams = InstanceConfig(capability="eventbus", name="side", driver="streams")
    assert "durable" in drivers["streams"](streams).features()
