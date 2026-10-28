"""RedisStreamsTransport integration tests (real Redis Streams).

Skipped when CACHE_URL is unset or Redis is unreachable. Proves durable
at-least-once over Streams: published entries persist, unacked entries are
redelivered to the same consumer (after a retry backoff), acked entries are not,
poison entries reach the DLQ only after the backoff schedule, and DLQ entries can
be replayed.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import orjson
import pytest

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.eventing.transport_base import DLQ_REASON_SOURCE_MISSING
from sandloom.core.testing import reload_test_env

redis_asyncio = pytest.importorskip("redis.asyncio")


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _env(event_id: str) -> EventEnvelope:
    return EventEnvelope(
        id=event_id,
        channel="core.test.v1",
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": 1},
    )


@pytest.fixture
async def redis_stream():
    cache_url = os.getenv("CACHE_URL")
    if not cache_url:
        pytest.skip("CACHE_URL not set; Redis Streams integration skipped")
    client = redis_asyncio.from_url(cache_url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    stream = "test.transport.stream"
    await client.delete(stream, f"{stream}.dlq")

    async def _factory():
        return client

    yield _factory, stream, client
    await client.delete(stream, f"{stream}.dlq")
    await client.aclose()


@pytest.fixture
async def transport(redis_stream):
    factory, stream, client = redis_stream
    # No backoff: redelivery semantics only (backoff has its own tests).
    return (
        RedisStreamsTransport(client_factory=factory, retry_base_ms=0),
        stream,
        client,
    )


@pytest.mark.asyncio
async def test_streams_at_least_once_redelivery_until_ack(transport) -> None:
    t, stream, _client = transport
    await t.ensure_group(stream, "g")
    await t.publish(stream, _env("e1"))
    await t.publish(stream, _env("e2"))

    batch = await t.consume(stream, group="g", consumer="c1", count=10)
    assert sorted(d.envelope["id"] for d in batch) == ["e1", "e2"]

    # Ack one; the other is still pending and must be redelivered.
    await t.ack(batch[0])
    redelivered = await t.consume(stream, group="g", consumer="c1", count=10)
    assert [d.envelope["id"] for d in redelivered] == [batch[1].envelope["id"]]

    # Ack the rest; nothing pending or new remains.
    await t.ack(redelivered[0])
    assert await t.consume(stream, group="g", consumer="c1", count=10) == []


@pytest.mark.asyncio
async def test_streams_event_published_while_offline_is_delivered(transport) -> None:
    t, stream, _client = transport
    # Group created before any consumer reads; publish "while offline".
    await t.ensure_group(stream, "g")
    await t.publish(stream, _env("late"))
    # Consumer comes online later and still receives it (not lost like Pub/Sub).
    batch = await t.consume(stream, group="g", consumer="c1", count=10)
    assert [d.envelope["id"] for d in batch] == ["late"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_malformed_entry_does_not_discard_healthy_batch(
    transport, pending: bool
) -> None:
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    await client.xadd(stream, {"data": "{not-json"})
    if pending:
        await client.xreadgroup("g", "c1", {stream: ">"}, count=1)
    await t.publish(stream, _env("healthy"))

    batch = await t.consume(stream, group="g", consumer="c1", count=10)

    assert [d.envelope["id"] for d in batch] == ["healthy"]
    dead = await t.list_dlq(stream)
    assert len(dead) == 1
    assert dead[0]["reason"] == "malformed_envelope"


@pytest.mark.asyncio
@pytest.mark.parametrize("max_deliveries", [1, 5])
async def test_targeted_replay_is_skipped_after_failed_ack(
    transport, max_deliveries: int
) -> None:
    t, stream, client = transport
    t._max_deliveries = max_deliveries
    await t.ensure_group(stream, "other")
    msg_id = await client.xadd(
        stream,
        {"data": orjson.dumps(_env("targeted")).decode(), "target_group": "owner"},
    )
    # Simulate the other group reading the replay and dying before ack-skip.
    await client.xreadgroup("other", "c1", {stream: ">"}, count=1)

    batch = await t.consume(stream, group="other", consumer="c1", count=10)

    assert batch == []
    pending = await client.xpending_range(stream, "other", "-", "+", 10)
    assert not any(p["message_id"] == msg_id for p in pending)


@pytest.mark.asyncio
async def test_own_retry_is_not_hidden_behind_other_consumers_pending(
    transport,
) -> None:
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    for n in range(300):
        await t.publish(stream, _env(f"other-{n}"))
    await client.xreadgroup("g", "other", {stream: ">"}, count=300)
    await t.publish(stream, _env("mine"))
    await client.xreadgroup("g", "c1", {stream: ">"}, count=1)

    batch = await t.consume(stream, group="g", consumer="c1", count=10)

    assert [d.envelope["id"] for d in batch] == ["mine"]


@pytest.mark.asyncio
async def test_explicit_dlq_id_is_found_beyond_first_scan_window(transport) -> None:
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    wanted = ""
    for n in range(8):
        wanted = await client.xadd(
            f"{stream}.dlq",
            {"group": "g", "data": orjson.dumps(_env(f"dlq-{n}")).decode()},
        )

    assert await t.replay_dlq(stream, ids=[wanted], count=1) == 1
    batch = await t.consume(stream, group="g", consumer="c1")
    assert [d.envelope["id"] for d in batch] == ["dlq-7"]


@pytest.mark.asyncio
async def test_pending_scan_advances_past_own_backing_off_head(transport) -> None:
    t, stream, client = transport
    t._retry_base_ms = 60_000
    t._min_idle_ms = 1_000_000
    await t.ensure_group(stream, "g")
    ids = []
    for n in range(300):
        ids.append(await t.publish(stream, _env(f"item-{n}")))
    await client.xreadgroup("g", "c1", {stream: ">"}, count=300)
    await client.xclaim(stream, "g", "c1", 0, ids[256:], idle=90_000, justid=True)
    assert await t.consume(stream, group="g", consumer="c1", count=1) == []
    batch = await t.consume(stream, group="g", consumer="c1", count=1)
    assert [d.envelope["id"] for d in batch] == ["item-256"]


@pytest.mark.asyncio
async def test_live_lease_renewal_prevents_another_replica_reclaim(
    redis_stream,
) -> None:
    from sandloom.core.eventing.streams_bus import StreamsEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    factory, _stream, client = redis_stream
    channel = "core.test.v1"
    # Use a separate valid channel because the production bus validates names.
    await client.delete(channel)
    t = RedisStreamsTransport(
        client_factory=factory,
        retry_base_ms=0,
        min_idle_ms=60,
        reclaim_interval_seconds=0,
    )
    other = RedisStreamsTransport(
        client_factory=factory,
        retry_base_ms=0,
        min_idle_ms=60,
        reclaim_interval_seconds=0,
    )
    bus = StreamsEventBus(InMemorySchemaRegistry(), t, poll_interval=0.01)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(_envelope: EventEnvelope) -> None:
        started.set()
        await release.wait()

    await bus.subscribe(channel, handler, group="lease")
    await t.publish(channel, _env("held"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.15)
        assert (
            await other.consume(channel, group="lease", consumer="other", count=1) == []
        )
    finally:
        release.set()
        await bus.close()
        await client.delete(channel, f"{channel}.dlq")


@pytest.mark.asyncio
async def test_lost_lease_cancels_handler_without_stealing_successors_entry(
    redis_stream,
) -> None:
    from sandloom.core.eventing.streams_bus import StreamsEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    factory, _stream, client = redis_stream
    channel = "core.lease_loss.v1"
    await client.delete(channel)
    transport = RedisStreamsTransport(
        client_factory=factory,
        retry_base_ms=0,
        min_idle_ms=60,
        reclaim_interval_seconds=0,
    )
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, poll_interval=0.01)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(_envelope: EventEnvelope) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await bus.subscribe(channel, handler, group="lease")
    event_id = await transport.publish(channel, _env("held"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await client.xclaim(channel, "lease", "successor", 0, [event_id], justid=True)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        pending = await client.xpending_range(channel, "lease", "-", "+", 10)
        assert pending[0]["consumer"] == "successor"
    finally:
        await bus.close()
        await client.delete(channel, f"{channel}.dlq")


@pytest.mark.asyncio
async def test_failed_entry_is_not_redelivered_before_its_backoff(redis_stream) -> None:
    """H2: a failing handler must not be retried (and DLQ'd) in a tight loop.

    Load-independent: "not yet" is only asserted while the backoff has
    provably not elapsed (measured with a monotonic clock), and redelivery is
    awaited with a deadline instead of a fixed sleep.
    """
    backoff_s = 1.0
    factory, stream, client = redis_stream
    t = RedisStreamsTransport(
        client_factory=factory,
        retry_base_ms=int(backoff_s * 1000),
        retry_max_ms=int(backoff_s * 1000),
        max_deliveries=2,
    )
    await t.ensure_group(stream, "g")
    await t.publish(stream, _env("flaky"))

    async def _consume_until(predicate, deadline_s: float) -> list:
        deadline = time.monotonic() + deadline_s
        while True:
            batch = await t.consume(stream, group="g", consumer="c1")
            if predicate(batch) or time.monotonic() >= deadline:
                return batch
            await asyncio.sleep(0.05)

    first = await t.consume(stream, group="g", consumer="c1")
    failed_at = time.monotonic()
    assert [d.envelope["id"] for d in first] == ["flaky"]
    # Handler failed (no ack). Re-consumes inside the backoff window must NOT
    # redeliver it and must NOT burn its budget into the DLQ.
    checks = 0
    while time.monotonic() - failed_at < backoff_s * 0.5:
        assert await t.consume(stream, group="g", consumer="c1") == []
        checks += 1
    assert checks >= 1
    assert await client.xlen(f"{stream}.dlq") == 0

    second = await _consume_until(bool, deadline_s=backoff_s * 10)
    assert [d.envelope["id"] for d in second] == ["flaky"]
    redelivered_at = time.monotonic()
    assert redelivered_at - failed_at >= backoff_s * 0.9

    # Budget (2 deliveries) used: after its backoff it is dead-lettered.
    if time.monotonic() - redelivered_at < backoff_s * 0.5:
        assert await t.consume(stream, group="g", consumer="c1") == []
        assert await client.xlen(f"{stream}.dlq") == 0
    deadline = time.monotonic() + backoff_s * 10
    while await client.xlen(f"{stream}.dlq") == 0 and time.monotonic() < deadline:
        assert await t.consume(stream, group="g", consumer="c1") == []
        await asyncio.sleep(0.05)
    assert await client.xlen(f"{stream}.dlq") == 1


@pytest.mark.asyncio
async def test_blocking_consume_waits_for_new_entries(transport) -> None:
    t, stream, _client = transport
    await t.ensure_group(stream, "g")

    async def _late_publish() -> None:
        await asyncio.sleep(0.1)
        await t.publish(stream, _env("wake"))

    task = asyncio.create_task(_late_publish())
    batch = await t.consume(stream, group="g", consumer="c1", block_ms=2_000)
    await task
    assert [d.envelope["id"] for d in batch] == ["wake"]


@pytest.mark.asyncio
async def test_dlq_replay_republishes_and_clears(redis_stream) -> None:
    factory, stream, client = redis_stream
    t = RedisStreamsTransport(client_factory=factory, retry_base_ms=0, max_deliveries=1)
    await t.ensure_group(stream, "g")
    await t.publish(stream, _env("poison"))
    assert [
        d.envelope["id"] for d in await t.consume(stream, group="g", consumer="c1")
    ] == ["poison"]
    # Budget exhausted -> DLQ.
    assert await t.consume(stream, group="g", consumer="c1") == []
    listed = await t.list_dlq(stream)
    assert [entry["event_id"] for entry in listed] == ["poison"]
    assert listed[0]["replayable"] is True

    assert await t.replay_dlq(stream) == 1
    assert await client.xlen(f"{stream}.dlq") == 0
    # Fresh delivery with the SAME envelope id (consumers dedupe on it).
    again = await t.consume(stream, group="g", consumer="c1")
    assert [d.envelope["id"] for d in again] == ["poison"]


@pytest.mark.asyncio
async def test_trimmed_entry_dead_letters_with_reason_marker(redis_stream) -> None:
    """Low: a pending entry trimmed from the stream is recorded with a reason,
    never as an empty payload masquerading as an event."""
    factory, stream, client = redis_stream
    t = RedisStreamsTransport(client_factory=factory, retry_base_ms=0, max_deliveries=1)
    await t.ensure_group(stream, "g")
    msg_id = await t.publish(stream, _env("gone"))
    await t.consume(stream, group="g", consumer="c1")
    await client.xdel(stream, msg_id)  # trimmed while pending

    assert await t.consume(stream, group="g", consumer="c1") == []
    entries: list[Any] = await client.xrange(f"{stream}.dlq")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["reason"] == DLQ_REASON_SOURCE_MISSING
    assert "data" not in fields
    # Not replayable: left in the DLQ.
    assert await t.replay_dlq(stream) == 0
    assert await client.xlen(f"{stream}.dlq") == 1


@pytest.mark.asyncio
async def test_consumer_recovers_after_stream_key_deleted(transport) -> None:
    """FLUSHDB / DEL of the stream drops the group; consume must recreate it.

    Without recovery XREADGROUP fails with NOGROUP forever because the
    transport cached "group created". The event published AFTER the deletion
    (which re-creates the bare stream without any group) must still be
    delivered, so the group is recreated at its last delivered position (not
    "$"); see test_streams_group_recreate.py for why not "0".
    """
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    await t.publish(stream, _env("before"))
    batch = await t.consume(stream, group="g", consumer="c1", count=10)
    assert [d.envelope["id"] for d in batch] == ["before"]
    await t.ack(batch[0])

    await client.delete(stream)  # simulates FLUSHDB: key + group gone
    await t.publish(stream, _env("after"))

    batch = await t.consume(stream, group="g", consumer="c1", count=10)
    assert [d.envelope["id"] for d in batch] == ["after"]
    await t.ack(batch[0])
    assert await t.consume(stream, group="g", consumer="c1", count=10) == []


@pytest.mark.asyncio
async def test_consumer_recovers_after_group_destroyed(transport) -> None:
    """Only the group is gone (stream key kept): recreate it and keep consuming."""
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    assert await t.consume(stream, group="g", consumer="c1", count=10) == []

    await client.xgroup_destroy(stream, "g")
    await t.publish(stream, _env("e1"))

    batch = await t.consume(stream, group="g", consumer="c1", count=10, block_ms=50)
    assert [d.envelope["id"] for d in batch] == ["e1"]


@pytest.mark.asyncio
async def test_consume_with_deleted_stream_and_no_new_events_returns_empty(
    transport,
) -> None:
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    await client.delete(stream)
    assert await t.consume(stream, group="g", consumer="c1", count=10) == []
    # The group exists again, so the next publish is delivered.
    await t.publish(stream, _env("e2"))
    batch = await t.consume(stream, group="g", consumer="c1", count=10)
    assert [d.envelope["id"] for d in batch] == ["e2"]


class _CallLog:
    """The Redis client, recording the name of every command method called."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[str] = []
        self.fail_after_xreadgroup = False

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._client, name)
        if not callable(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            result = await target(*args, **kwargs)
            if name == "xreadgroup" and self.fail_after_xreadgroup:
                self.fail_after_xreadgroup = False
                raise ConnectionError("reply lost after Redis applied XREADGROUP")
            return result

        return call

    def count(self, name: str) -> int:
        return self.calls.count(name)


def _logged_transport(
    client: Any, **kwargs: Any
) -> tuple[RedisStreamsTransport, _CallLog]:
    log = _CallLog(client)

    async def _factory() -> Any:
        return log

    kwargs.setdefault("retry_base_ms", 0)
    kwargs.setdefault("reclaim_interval_seconds", 3600)
    return RedisStreamsTransport(client_factory=_factory, **kwargs), log


@pytest.mark.asyncio
async def test_idle_and_fully_acked_polls_skip_xpending(redis_stream) -> None:
    _factory, stream, client = redis_stream
    t, log = _logged_transport(client)
    await t.ensure_group(stream, "g")
    assert await t.consume(stream, group="g", consumer="c1") == []
    assert log.count("xpending_range") == 1  # the first scan learns the PEL

    log.calls.clear()
    for _ in range(3):  # idle polls
        assert await t.consume(stream, group="g", consumer="c1") == []
    await t.publish(stream, _env("e1"))
    batch = await t.consume(stream, group="g", consumer="c1")
    await t.ack_many(batch)
    assert await t.consume(stream, group="g", consumer="c1") == []
    assert log.count("xpending_range") == 0
    assert log.count("xreadgroup") == 5


@pytest.mark.asyncio
async def test_failed_entry_is_still_rescanned_and_redelivered(redis_stream) -> None:
    _factory, stream, client = redis_stream
    t, log = _logged_transport(client)
    await t.ensure_group(stream, "g")
    await t.consume(stream, group="g", consumer="c1")
    for n in (1, 2):
        await t.publish(stream, _env(f"e{n}"))
    first = await t.consume(stream, group="g", consumer="c1")
    await t.ack(first[1])  # e1's handler failed: left unacked

    log.calls.clear()
    again = await t.consume(stream, group="g", consumer="c1")
    assert [d.envelope["id"] for d in again] == ["e1"]
    assert log.count("xpending_range") == 1

    # Still failing: the cursor sits on e1 and nothing lies past it, so the
    # window wraps to the head in ONE XPENDING (it used to take two).
    log.calls.clear()
    third = await t.consume(stream, group="g", consumer="c1")
    assert [d.envelope["id"] for d in third] == ["e1"]
    assert log.count("xpending_range") == 1
    await t.ack_many(third)

    log.calls.clear()
    assert await t.consume(stream, group="g", consumer="c1") == []
    assert log.count("xpending_range") == 0


@pytest.mark.asyncio
async def test_lost_xreadgroup_reply_forces_a_rescan(redis_stream) -> None:
    _factory, stream, client = redis_stream
    t, log = _logged_transport(client)
    await t.ensure_group(stream, "g")
    await t.consume(stream, group="g", consumer="c1")
    await t.publish(stream, _env("lost"))
    log.fail_after_xreadgroup = True
    with pytest.raises(ConnectionError):
        await t.consume(stream, group="g", consumer="c1")

    batch = await t.consume(stream, group="g", consumer="c1")
    assert [d.envelope["id"] for d in batch] == ["lost"]


@pytest.mark.asyncio
async def test_forced_pending_scan_finds_entries_claimed_into_our_pel(
    redis_stream,
) -> None:
    _factory, stream, client = redis_stream
    t, _log = _logged_transport(client, reclaim_interval_seconds=0)
    await t.ensure_group(stream, "g")
    await t.consume(stream, group="g", consumer="c1")
    msg_id = await t.publish(stream, _env("foreign"))
    await client.xreadgroup("g", "other", {stream: ">"}, count=1)
    await client.xclaim(stream, "g", "c1", 0, [msg_id], justid=True)

    batch = await t.consume(stream, group="g", consumer="c1")
    assert [d.envelope["id"] for d in batch] == ["foreign"]


@pytest.mark.asyncio
async def test_read_batch_decodes_each_entry_once_and_notes_the_last(
    transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.eventing import streams_consume as transport_mod

    t, stream, _client = transport
    await t.ensure_group(stream, "g")
    ids = [await t.publish(stream, _env(f"e{n}")) for n in range(10)]
    loads: list[Any] = []
    real_loads = transport_mod.orjson.loads

    class _Counting:
        @staticmethod
        def loads(raw: Any) -> Any:
            loads.append(raw)
            return real_loads(raw)

        dumps = staticmethod(transport_mod.orjson.dumps)

    monkeypatch.setattr(transport_mod, "orjson", _Counting)
    batch = await t.consume(stream, group="g", consumer="c1", count=10)

    assert len(batch) == 10
    assert len(loads) == 10  # was 20: the position note decoded every entry
    assert t._last_delivered[(stream, "g")] == (ids[-1], "e9")


@pytest.mark.asyncio
async def test_position_is_noted_when_the_last_entry_is_skipped(transport) -> None:
    t, stream, client = transport
    await t.ensure_group(stream, "g")
    await t.publish(stream, _env("mine"))
    last = await client.xadd(
        stream,
        {"data": orjson.dumps(_env("theirs")).decode(), "target_group": "owner"},
    )
    batch = await t.consume(stream, group="g", consumer="c1", count=10)

    assert [d.envelope["id"] for d in batch] == ["mine"]
    assert t._last_delivered[(stream, "g")] == (last, "theirs")
