"""Declared transport capabilities (review map B-25).

The Streams bus resolves what its transport can do once, from the
transport class's own ``capabilities`` declaration, and probes an undeclared
transport exactly as it used to feature-detect it.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.eventing.transport_capabilities import (
    TransportCapability,
    probe_transport_capabilities,
    transport_capabilities,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry

Cap = TransportCapability


async def _client() -> Any:  # pragma: no cover - never called
    raise AssertionError("no I/O in capability resolution")


@pytest.mark.parametrize(
    "transport",
    [MemoryTransport(), RedisStreamsTransport(client_factory=_client)],
    ids=["memory", "redis-streams"],
)
def test_builtin_declarations_match_what_probing_finds(transport: Any) -> None:
    """A declaration may not drift from the methods the class really has."""
    declared = type(transport).capabilities
    assert declared is not None
    assert declared == probe_transport_capabilities(transport)
    assert transport_capabilities(transport) == declared


def test_builtin_capability_tables() -> None:
    assert transport_capabilities(MemoryTransport()) == frozenset(Cap) - {
        Cap.NATIVE_BATCH_PUBLISH,
        Cap.LEASE_RENEWAL,
    }
    redis = RedisStreamsTransport(client_factory=_client)
    assert transport_capabilities(redis) == frozenset(Cap)


def test_subclass_without_declaration_is_probed() -> None:
    """A declaration is not inherited: a subclass may add or narrow methods."""

    class _Leased(MemoryTransport):
        renew_interval_seconds = 1.0

        async def renew_pending(self, deliveries: Any, consumer: str) -> None:
            return None

        async def consume(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    caps = transport_capabilities(_Leased())
    assert Cap.LEASE_RENEWAL in caps
    assert Cap.BLOCKING_CONSUME not in caps  # *args/**kwargs: not detectable


def test_duck_typed_transport_gets_the_conservative_default() -> None:
    class _Minimal:
        async def publish(self, stream: str, envelope: Any) -> str:
            return "1-0"

        async def consume(self, stream: str, *, group: str, consumer: str) -> list[Any]:
            return []

        async def ensure_group(self, stream: str, group: str) -> None:
            return None

        async def ack(self, delivery: Any) -> None:
            return None

    assert transport_capabilities(_Minimal()) == frozenset()
    bus = StreamsEventBus(InMemorySchemaRegistry(), _Minimal())
    assert bus.transport_capabilities == frozenset()
    assert bus.native_batch_publish is False
    assert bus._batch_lease("c.v1", "g", "c1") is None


def test_an_explicit_declaration_wins_over_the_methods() -> None:
    class _Declared(MemoryTransport):
        capabilities = frozenset({Cap.BATCH_ACK})

    bus = StreamsEventBus(InMemorySchemaRegistry(), _Declared())
    assert bus.transport_capabilities == frozenset({Cap.BATCH_ACK})
    assert bus.native_batch_publish is False
