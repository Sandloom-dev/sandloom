"""Split-deployment eventing: the kernel's side, on the memory transport.

The Redis Streams half of this suite (two services sharing one broker, retention,
``start_from``, lag, the DLQ replay targeted at one group, the inventory, the key
prefix) runs against a real Redis in the ``sandloom-redis`` package:
``packages/sandloom-redis/tests/test_redis_eventbus_split_identity.py``.

* H1 — two handlers resolving to one group inside a process fail startup, and the
  service name is part of the consumer id.
* M1 — a DLQ replay is delivered to the ORIGINAL group only (memory transport).
* C-EVENT-01 — ``publish_event_versions`` is re-exported for modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.consumers.runtime import ConsumerConfigurationError, ConsumerRuntime
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.tests._schemas import PermissiveSchemaRegistry


@pytest.mark.asyncio
async def test_duplicate_group_in_one_process_fails_startup() -> None:
    """Two handlers resolving to one group: the second would never run."""
    from sandloom.core.consumers.callbacks import EventConsumerDescriptor

    async def _h(_s: Any, _e: Any) -> None:
        return None

    bus = StreamsEventBus(PermissiveSchemaRegistry(), MemoryTransport())
    core = CoreFacade(app=FastAPI())
    core.set_event_bus(bus)
    for _ in range(2):  # direct facade registration bypasses the draft preflight
        core.register_event_consumer(
            EventConsumerDescriptor(name="dup", channel="demo.x.y.v1", handler=_h)
        )

    @asynccontextmanager
    async def _opener(_name: str) -> AsyncIterator[Any]:
        yield object()

    runtime = ConsumerRuntime(
        core, session_opener=_opener, startup_timeout=1.0, retry_delay=0.01
    )
    with pytest.raises(ConsumerConfigurationError, match="same consumer group"):
        await runtime.start()


def test_service_name_is_part_of_consumer_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.eventing import transport_base as transport_transport_base

    monkeypatch.setattr(settings, "sandloom_service_name", "billing-api")
    assert transport_transport_base.process_consumer_id().startswith("billing-api@")
    monkeypatch.setattr(settings, "sandloom_service_name", None)
    assert "@" not in transport_transport_base.process_consumer_id()


@pytest.mark.asyncio
async def test_memory_dlq_replay_is_targeted_too() -> None:
    t = MemoryTransport(max_deliveries=1)
    await t.ensure_group("s.x.v1", "ok")
    await t.ensure_group("s.x.v1", "bad")
    await t.publish("s.x.v1", {"id": "e1", "payload": {}})  # type: ignore[typeddict-item]
    for d in await t.consume("s.x.v1", group="ok", consumer="c"):
        await t.ack(d)
    await t.consume("s.x.v1", group="bad", consumer="c")
    await t.consume("s.x.v1", group="bad", consumer="c")  # -> DLQ
    assert (await t.list_dlq("s.x.v1"))[0].group == "bad"
    assert await t.replay_dlq("s.x.v1", group="ok") == 0  # not ok's entry
    assert await t.replay_dlq("s.x.v1") == 1
    assert await t.consume("s.x.v1", group="ok", consumer="c") == []
    assert [
        d.envelope["id"] for d in await t.consume("s.x.v1", group="bad", consumer="c")
    ] == ["e1"]


def test_publish_event_versions_is_reexported_for_modules() -> None:
    """Modules may import only ``sandloom.core.di`` — the helper must be there."""
    from sandloom.core import di
    from sandloom.core.unit_of_work import versioning

    assert di.publish_event_versions is versioning.publish_event_versions
    assert "publish_event_versions" in di.__all__


@pytest.mark.asyncio
async def test_memory_dlq_continuation_uses_numeric_stream_order() -> None:
    transport = MemoryTransport()
    transport._dlq["paging.v1"] = [(f"{i}-0", {}, 1, "group") for i in range(1, 13)]
    first = await transport.list_dlq("paging.v1", count=9)
    second = await transport.list_dlq("paging.v1", after=first[-1].id)
    assert [row.id for row in second] == ["10-0", "11-0", "12-0"]
