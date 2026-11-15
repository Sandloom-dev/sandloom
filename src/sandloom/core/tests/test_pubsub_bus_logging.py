"""The broadcast bus logs a failure by its type, never the driver's error text.

A broadcast driver's error names the host and port (``redis://user:pw@host``)
and a payload can ride in it; ``logger.exception`` wrote all of that, with the
traceback, into the log. These tests put a secret-bearing error through each
log site and look for it in the records.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.schema.registry import InMemorySchemaRegistry

SECRET = "redis://user:hunter2@10.9.8.7:6379/0 payload=card-4242"
LOGGER = "sandloom.core.eventing.event_bus"


class _FailingBroadcast:
    async def publish(self, channel: str, payload: bytes) -> None:
        raise ConnectionError(SECRET)


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        id="e1",
        channel="core.thing.v1",
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": 1},
    )


def _assert_type_only(caplog: pytest.LogCaptureFixture, type_name: str) -> None:
    records = [r for r in caplog.records if r.name == LOGGER]
    assert records, "the failure was not logged"
    for record in records:
        assert record.levelno >= logging.ERROR
        assert record.exc_info is None, "no traceback: it carries the error text"
        assert type_name in record.getMessage()
        assert "hunter2" not in caplog.text
        assert "10.9.8.7" not in caplog.text
        assert "card-4242" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_broadcast_publish_logs_the_type_and_still_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = RedisEventBus(InMemorySchemaRegistry(), broadcast=_FailingBroadcast())
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        with pytest.raises(ConnectionError):  # the relay must see it and retry
            await bus._publish_to_transport("core.thing.v1", _envelope())
    _assert_type_only(caplog, "ConnectionError")


@pytest.mark.asyncio
async def test_a_failed_delivery_logs_the_type_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = RedisEventBus(InMemorySchemaRegistry())

    async def boom(*_args: Any) -> None:
        raise ConnectionError(SECRET)

    monkeypatch.setattr(bus, "_pubsub_envelope", lambda *_a: _envelope())
    monkeypatch.setattr(bus, "_fan_out", boom)
    message = SimpleNamespace(channel="core.thing.v1", payload=b"{}")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bus._deliver("core.thing.v1", message)
    _assert_type_only(caplog, "ConnectionError")


@pytest.mark.asyncio
async def test_a_failed_callback_logs_the_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = RedisEventBus(InMemorySchemaRegistry())

    async def callback(_envelope: EventEnvelope) -> None:
        raise ValueError(SECRET)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bus._invoke_callback(callback, "core.thing.v1", _envelope())
    _assert_type_only(caplog, "ValueError")
