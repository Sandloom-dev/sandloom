"""Shared doubles for the pipelined-delivery tests (not a test module)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.transport_base import TransportBatchError
from sandloom.core.schema.registry import InMemorySchemaRegistry

CHANNEL = "core.test_event.v1"


class RecordingTransport(MemoryTransport):
    """MemoryTransport counting batch calls; can fail a batch on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.single: list[str] = []
        self.fail_next: BaseException | None = None
        self.partial_next: int | None = None
        self.delay: float = 0.0
        self.gate: asyncio.Event | None = None
        self.on_batch: Any = None

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        self.single.append(str(envelope["id"]))
        return await super().publish(stream, envelope)

    async def publish_many(
        self, items: Sequence[tuple[str, EventEnvelope]]
    ) -> list[str]:
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.on_batch is not None:
            await self.on_batch(items)
        self.batches.append([str(env["id"]) for _s, env in items])
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise TransportBatchError([], f"batch failed: {exc}") from exc
        if self.partial_next is not None:
            keep, self.partial_next = self.partial_next, None
            ids = [self._append(s, e, target_group=None) for s, e in items[:keep]]
            raise TransportBatchError(ids, "WRONGTYPE") from RuntimeError("WRONGTYPE")
        return [self._append(s, e, target_group=None) for s, e in items]

    def stream_ids(self, stream: str = CHANNEL) -> list[str]:
        return [str(env["id"]) for _mid, env in self._log.get(stream, [])]


def streams_bus(transport: MemoryTransport) -> StreamsEventBus:
    registry = InMemorySchemaRegistry()
    registry.register(
        CHANNEL,
        "test",
        {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    return StreamsEventBus(registry, transport, poll_interval=0.01)
