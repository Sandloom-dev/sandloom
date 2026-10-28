from __future__ import annotations

import pytest

from sandloom.core.eventing.bus_metrics import get_event_bus_metrics
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade


@pytest.mark.asyncio
async def test_publish_without_schema_is_refused(bus: InMemoryEventBus):
    with pytest.raises(ValueError, match="Missing schema for versioned channel"):
        await bus.publish_event("foo.bar.v1", {"a": 1})


@pytest.mark.asyncio
async def test_validation_failure_lists_every_violation(
    core_facade: CoreFacade, bus: InMemoryEventBus
):
    channel = "acme.many.v1"
    core_facade.register_event_schema(
        channel=channel,
        name="many",
        schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
            "required": ["a", "b", "c"],
        },
    )
    with pytest.raises(ValueError) as excinfo:
        await bus.publish_event(channel, {"a": "x", "b": 1})
    message = str(excinfo.value)
    assert "is a required property" in message and "c" in message
    assert "(at /a)" in message and "(at /b)" in message
    assert "SANDLOOM-R004" in message


@pytest.mark.asyncio
async def test_schema_validation_success(
    monkeypatch: pytest.MonkeyPatch, core_facade: CoreFacade, bus: InMemoryEventBus
):
    channel = "acme.orders.v1"
    core_facade.register_event_schema(
        channel=channel,
        name="order_payload",
        schema={
            "type": "object",
            "properties": {"a": {"type": "number"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )

    async def nop(_: dict[str, object]):
        return None

    await bus.subscribe(channel, nop)
    await bus.publish_event(channel, {"a": 10})  # should not raise


@pytest.mark.asyncio
async def test_schema_validation_failure(
    monkeypatch: pytest.MonkeyPatch, core_facade: CoreFacade, bus: InMemoryEventBus
):
    channel = "acme.users.v1"
    core_facade.register_event_schema(
        channel=channel,
        name="user_payload",
        schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    )

    async def nop(_: dict[str, object]):
        return None

    await bus.subscribe(channel, nop)
    with pytest.raises(ValueError):
        await bus.publish_event(channel, {"wrong": 1})


@pytest.mark.asyncio
async def test_schema_validation_metrics_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, core_facade: CoreFacade, bus: InMemoryEventBus
):
    # Capture baseline metrics
    before = get_event_bus_metrics()
    success_channel = "metrics.success.v1"
    fail_channel = "metrics.fail.v1"
    core_facade.register_event_schema(
        channel=success_channel,
        name="success_payload",
        schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
            "additionalProperties": False,
        },
    )
    core_facade.register_event_schema(
        channel=fail_channel,
        name="fail_payload",
        schema={
            "type": "object",
            "properties": {"y": {"type": "string"}},
            "required": ["y"],
            "additionalProperties": False,
        },
    )

    async def nop(_: dict[str, object]):
        return None

    await bus.subscribe(success_channel, nop)
    await bus.subscribe(fail_channel, nop)
    # Success publish
    await bus.publish_event(success_channel, {"x": 1})
    # Failure publish
    with pytest.raises(ValueError):
        await bus.publish_event(fail_channel, {"wrong": 1})
    after = get_event_bus_metrics()
    assert after["schema_validation_success"] - before["schema_validation_success"] >= 1
    assert after["schema_validation_failure"] - before["schema_validation_failure"] >= 1
