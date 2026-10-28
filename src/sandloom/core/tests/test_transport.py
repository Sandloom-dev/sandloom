"""MemoryTransport + ConsumerInbox: at-least-once delivery and dedupe.

Proves the foundation for durable domain events: a message stays deliverable
until acked (so an offline consumer never loses it), and the consumer inbox
turns at-least-once redelivery into once-per-effect on the stable envelope id.
"""

from __future__ import annotations

import pytest

from sandloom.core.eventing.consumer_inbox import ConsumerInbox
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_transport import RedisStreamsTransport


def _env(event_id: str, channel: str = "core.test.v1") -> EventEnvelope:
    return EventEnvelope(
        id=event_id,
        channel=channel,
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": 1},
    )


@pytest.mark.asyncio
async def test_reclaim_advances_server_scan_cursor() -> None:
    starts = []

    class Client:
        async def xautoclaim(self, *args, **kwargs):
            starts.append(kwargs["start_id"])
            return ["99-0" if len(starts) == 1 else "0-0", [], []]

    transport = RedisStreamsTransport(reclaim_interval_seconds=0)
    client = Client()
    for _ in range(3):
        await transport._reclaim_stale(client, "s", "g", "c", 1)
    assert starts == ["0-0", "99-0", "0-0"]


def _maintenance_failures(stream: str, operation: str) -> float:
    from sandloom.core.observability import metrics

    return float(
        metrics.streams_maintenance_failures_total.labels(
            stream=stream, operation=operation
        )._value.get()
    )


@pytest.mark.asyncio
async def test_failed_reclaim_is_counted() -> None:
    class Client:
        async def xautoclaim(self, *args, **kwargs):
            raise ConnectionError("redis down")

    transport = RedisStreamsTransport(reclaim_interval_seconds=0)
    before = _maintenance_failures("s-reclaim", "xautoclaim")
    await transport._reclaim_stale(Client(), "s-reclaim", "g", "c", 1)
    assert _maintenance_failures("s-reclaim", "xautoclaim") == before + 1


@pytest.mark.asyncio
async def test_failed_age_trim_is_counted() -> None:
    class Client:
        async def xtrim(self, *args, **kwargs):
            raise ConnectionError("redis down")

    transport = RedisStreamsTransport()
    transport._retention_seconds = 60
    transport._trim_interval = 0
    key = transport.key("s-trim")
    before = _maintenance_failures(key, "xtrim")
    await transport._maybe_trim_by_age(Client(), key)
    assert _maintenance_failures(key, "xtrim") == before + 1


@pytest.mark.asyncio
async def test_unacked_message_is_redelivered() -> None:
    t = MemoryTransport()
    await t.ensure_group("s", "g")
    await t.publish("s", _env("e1"))

    first = await t.consume("s", group="g", consumer="c1")
    assert [d.envelope["id"] for d in first] == ["e1"]
    # Not acked -> redelivered on the next consume.
    again = await t.consume("s", group="g", consumer="c1")
    assert [d.envelope["id"] for d in again] == ["e1"]
    assert t.pending_count("s", "g") == 1


@pytest.mark.asyncio
async def test_poison_dead_lettered_after_exactly_max_deliveries() -> None:
    """A handler that never acks is redelivered up to max_deliveries times, then
    dead-lettered — the budget counts handler-facing deliveries, not more."""
    t = MemoryTransport(max_deliveries=3)
    await t.ensure_group("s", "g")
    await t.publish("s", _env("poison"))

    # Deliveries 1..3 hand the message to the handler (it never acks).
    for _ in range(3):
        batch = await t.consume("s", group="g", consumer="c1")
        assert [d.envelope["id"] for d in batch] == ["poison"]
        assert t.dlq_depth("s") == 0
    # The 4th consume crosses the budget: dead-lettered, no longer delivered.
    batch = await t.consume("s", group="g", consumer="c1")
    assert batch == []
    assert t.dlq_depth("s") == 1
    assert t.pending_count("s", "g") == 0


@pytest.mark.asyncio
async def test_message_acked_before_budget_is_not_dead_lettered() -> None:
    """Failing max_deliveries-1 times then succeeding must NOT dead-letter."""
    t = MemoryTransport(max_deliveries=3)
    await t.ensure_group("s", "g")
    await t.publish("s", _env("ok"))

    # Two failed deliveries (no ack), then succeed on the third.
    for _ in range(2):
        await t.consume("s", group="g", consumer="c1")
    batch = await t.consume("s", group="g", consumer="c1")
    await t.ack(batch[0])
    assert t.dlq_depth("s") == 0
    assert t.pending_count("s", "g") == 0


@pytest.mark.asyncio
async def test_acked_message_is_not_redelivered() -> None:
    t = MemoryTransport()
    await t.publish("s", _env("e1"))
    await t.publish("s", _env("e2"))

    batch = await t.consume("s", group="g", consumer="c1")
    assert [d.envelope["id"] for d in batch] == ["e1", "e2"]
    await t.ack(batch[0])
    # Only the unacked one remains.
    remaining = await t.consume("s", group="g", consumer="c1")
    assert [d.envelope["id"] for d in remaining] == ["e2"]
    assert t.pending_count("s", "g") == 1


@pytest.mark.asyncio
async def test_groups_are_independent() -> None:
    t = MemoryTransport()
    await t.publish("s", _env("e1"))
    a = await t.consume("s", group="ga", consumer="c1")
    b = await t.consume("s", group="gb", consumer="c1")
    # Each group gets its own copy of the stream.
    assert [d.envelope["id"] for d in a] == ["e1"]
    assert [d.envelope["id"] for d in b] == ["e1"]


@pytest.mark.asyncio
async def test_dlq_append_failure_leaves_source_message_unacked() -> None:
    """A poison entry must NOT be acked if its DLQ append failed.

    Acking it then would lose it forever: gone from the source pending list and
    never written to the DLQ. Leaving it un-acked lets XAUTOCLAIM reclaim and
    retry it once the broker recovers.
    """
    acked: list[tuple[str, str, str]] = []

    class _FailingDlqClient:
        async def xadd(self, *_a: object, **_k: object) -> str:
            raise RuntimeError("dlq down")

        async def xack(self, stream: str, group: str, msg_id: str) -> int:
            acked.append((stream, group, msg_id))
            return 1

    t = RedisStreamsTransport(client_factory=None)
    # _to_dlq takes the client directly, so no factory I/O is needed.
    await t._to_dlq(_FailingDlqClient(), "s", "g", "1-0", raw="{}", attempts=4)
    assert acked == []  # the source entry was NOT acked


@pytest.mark.asyncio
async def test_dlq_acks_source_only_after_successful_append() -> None:
    """On a successful DLQ append the source entry IS acked exactly once."""
    added: list[str] = []
    acked: list[str] = []

    class _OkClient:
        async def xadd(self, dlq: str, _fields: object) -> str:
            added.append(dlq)
            return "1-0"

        async def xack(self, _stream: str, _group: str, msg_id: str) -> int:
            acked.append(msg_id)
            return 1

    t = RedisStreamsTransport(client_factory=None)
    await t._to_dlq(_OkClient(), "s", "g", "7-0", raw="{}", attempts=4)
    assert added == ["s.dlq"]
    assert acked == ["7-0"]


@pytest.mark.asyncio
async def test_consumer_inbox_dedupes_on_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, str] = {}

    class _FakeRedis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return None
            store[key] = value
            return True

    async def _get_cache():
        return _FakeRedis()

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", _get_cache
    )
    inbox = ConsumerInbox("emailer")
    assert await inbox.seen("e1") is False  # first time -> process
    assert await inbox.seen("e1") is True  # duplicate -> skip
    assert await inbox.seen("e2") is False  # different id -> process


@pytest.mark.asyncio
async def test_consumer_inbox_fails_open_on_cache_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenRedis:
        async def set(self, *a, **k):
            raise RuntimeError("cache down")

    async def _get_cache():
        return _BrokenRedis()

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", _get_cache
    )
    inbox = ConsumerInbox("emailer")
    # Degrade to at-least-once (process) rather than dropping the event.
    assert await inbox.seen("e1") is False
