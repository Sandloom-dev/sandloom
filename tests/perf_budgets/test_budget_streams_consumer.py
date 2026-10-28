"""Budget: Redis round trips per consumed Streams batch (real Redis).

A consumed batch's inbox dedupe is ONE ``MGET``, the successful deliveries'
inbox marks ONE pipeline and their acks ONE multi-id ``XACK`` — 3 round trips
per batch whatever its size (per-message dispatch paid GET + SET + XACK = 3
per EVENT, i.e. 192 for a batch of 64). Round trips are counted on the redis
connections themselves (``send_packed_command``: one per command, one per
pipeline), so the number is deterministic.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.eventing import consumer_inbox as transport_mod
from sandloom.core.eventing.consumer_inbox import ConsumerInbox
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing.workers import worker_env_values

redis_asyncio = pytest.importorskip("redis.asyncio")
from redis.asyncio.connection import Connection  # noqa: E402

BATCH = 64


def _cache_url() -> str:
    env = worker_env_values(Path(__file__).resolve().parents[2] / ".env.test")
    url = os.environ.get("CACHE_URL") or env.get("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not configured")
    return str(url)


class _RoundTrips:
    def __init__(self) -> None:
        self.count = 0


@pytest_asyncio.fixture
async def redis_env(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, RedisCacheProvider, _RoundTrips, str]]:
    url = _cache_url()
    client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    stream = f"budget.stream.{uuid.uuid4().hex[:8]}.v1"
    cache = RedisCacheProvider(
        name="budget", driver="redis", options={"url": url}, shared=False
    )
    monkeypatch.setattr(transport_mod, "cache", lambda name="default": cache)
    trips = _RoundTrips()
    original = Connection.send_packed_command

    async def counting(self: Any, command: Any, check_health: bool = True) -> None:
        trips.count += 1
        await original(self, command, check_health)

    monkeypatch.setattr(Connection, "send_packed_command", counting)
    try:
        yield client, cache, trips, stream
    finally:
        await client.delete(stream, f"{stream}.dlq")
        keys = [k async for k in client.scan_iter(match=f"inbox:{stream}*")]
        if keys:
            await client.delete(*keys)
        await cache.aclose()
        await client.aclose()


def _envelope(stream: str) -> EventEnvelope:
    return EventEnvelope(
        id=str(uuid.uuid4()),
        channel=stream,
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": 1},
    )


@pytest.mark.asyncio
async def test_a_consumed_batch_costs_three_redis_round_trips(
    redis_env: tuple[Any, RedisCacheProvider, _RoundTrips, str],
) -> None:
    client, cache, trips, stream = redis_env

    async def _factory() -> Any:
        return client

    transport = RedisStreamsTransport(client_factory=_factory, retry_base_ms=0)
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, read_count=BATCH)
    # Open the inbox cache's connection first (connect handshake: SELECT +
    # CLIENT SETINFO are not per-batch work).
    await cache.get("budget-warmup")
    group = "budget-group"
    await transport.ensure_group(stream, group)
    for _ in range(BATCH):
        await transport.publish(stream, _envelope(stream))
    handled: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        handled.append(envelope["id"])

    bus._subs[(stream, group)] = [handler]  # the consume loop's registration
    inbox = ConsumerInbox(f"{stream}:{group}")

    trips.count = 0
    deliveries = await transport.consume(
        stream, group=group, consumer="c1", count=BATCH
    )
    consume_trips = trips.count
    assert len(deliveries) == BATCH

    trips.count = 0
    await bus._dispatch_batch(stream, group, deliveries, inbox)
    # BUDGET: MGET (dedupe) + one pipeline (marks) + one XACK — per BATCH.
    assert trips.count == 3
    assert len(handled) == BATCH
    # XAUTOCLAIM (throttled) + XPENDING (first scan) + XREADGROUP.
    assert consume_trips <= 3
    assert await transport.stats(stream, group) == {"pending": 0, "dlq": 0}

    # Redelivered duplicates are dropped by the batch's single MGET.
    trips.count = 0
    await bus._dispatch_batch(stream, group, deliveries, inbox)
    assert len(handled) == BATCH
    assert trips.count == 2  # MGET + XACK (nothing to mark)


@pytest.mark.asyncio
async def test_failed_deliveries_stay_pending_while_the_rest_is_acked(
    redis_env: tuple[Any, RedisCacheProvider, _RoundTrips, str],
) -> None:
    client, _cache, _trips, stream = redis_env

    async def _factory() -> Any:
        return client

    transport = RedisStreamsTransport(client_factory=_factory, retry_base_ms=0)
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, read_count=8)
    group = "budget-fail"
    await transport.ensure_group(stream, group)
    envelopes = [_envelope(stream) for _ in range(8)]
    for env in envelopes:
        await transport.publish(stream, env)
    poison = {envelopes[2]["id"], envelopes[5]["id"]}

    async def handler(envelope: EventEnvelope) -> None:
        if envelope["id"] in poison:
            raise RuntimeError("boom")

    bus._subs[(stream, group)] = [handler]
    inbox = ConsumerInbox(f"{stream}:{group}")
    deliveries = await transport.consume(stream, group=group, consumer="c1", count=8)
    await bus._dispatch_batch(stream, group, deliveries, inbox)
    # Only the 2 failures stay pending (redelivered later), unmarked.
    assert (await transport.stats(stream, group))["pending"] == 2
    assert (
        await inbox.processed_many([e["id"] for e in envelopes])
        == {e["id"] for e in envelopes} - poison
    )


_COMMAND = re.compile(rb"\*\d+\r\n\$\d+\r\n([A-Za-z]+)\r\n")


@pytest_asyncio.fixture
async def wire_trace(
    redis_env: tuple[Any, RedisCacheProvider, _RoundTrips, str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, RedisCacheProvider, list[list[str]], str]]:
    """Every send on the wire as the list of command names it carries."""
    client, cache, _trips, stream = redis_env
    sends: list[list[str]] = []
    counting = Connection.send_packed_command

    async def tracing(self: Any, command: Any, check_health: bool = True) -> None:
        chunks = [command] if isinstance(command, (bytes, str)) else list(command)
        raw = b"".join(c.encode() if isinstance(c, str) else bytes(c) for c in chunks)
        sends.append([m.decode().upper() for m in _COMMAND.findall(raw)])
        await counting(self, command, check_health)

    monkeypatch.setattr(Connection, "send_packed_command", tracing)
    yield client, cache, sends, stream


async def _one_delivery_cycle(
    bus: StreamsEventBus,
    transport: RedisStreamsTransport,
    stream: str,
    group: str,
    inbox: ConsumerInbox,
) -> None:
    await transport.publish(stream, _envelope(stream))
    deliveries = await transport.consume(stream, group=group, consumer="c1", count=8)
    assert len(deliveries) == 1
    await bus._dispatch_batch(stream, group, deliveries, inbox)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("durable", "expected"),
    [
        # The DB claim in the handler's UoW is the dedupe: no cache inbox.
        # No XPENDING either: every entry of the consumer's PEL was acked.
        (True, [["XADD"], ["XREADGROUP"], ["XACK"]]),
        # A non-durable consumer keeps the cache inbox (MGET + SET pipeline).
        (
            False,
            [
                ["XADD"],
                ["XREADGROUP"],
                ["MGET"],
                ["SET"],  # the marks' pipeline (one send; one mark here)
                ["XACK"],
            ],
        ),
    ],
)
async def test_redis_wire_per_delivery_durable_vs_cache_inbox(
    wire_trace: tuple[Any, RedisCacheProvider, list[list[str]], str],
    durable: bool,
    expected: list[list[str]],
) -> None:
    client, cache, sends, stream = wire_trace

    async def _factory() -> Any:
        return client

    # Reclaim throttled away from the measured cycle (it runs on the first).
    transport = RedisStreamsTransport(
        client_factory=_factory, retry_base_ms=0, reclaim_interval_seconds=3600
    )
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, read_count=8)
    await cache.get("wire-warmup")
    group = f"wire-{durable}"
    await transport.ensure_group(stream, group)
    handled: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        handled.append(envelope["id"])

    handler.sandloom_durable = durable  # type: ignore[attr-defined]
    bus._subs[(stream, group)] = [handler]
    inbox = ConsumerInbox(f"{stream}:{group}")
    await _one_delivery_cycle(bus, transport, stream, group, inbox)  # warm-up

    sends.clear()
    await _one_delivery_cycle(bus, transport, stream, group, inbox)

    assert [s for s in sends if s] == expected
    assert len(handled) == 2
    assert await transport.stats(stream, group) == {"pending": 0, "dlq": 0}
