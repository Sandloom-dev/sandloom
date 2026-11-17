"""MemoryTransport + ConsumerInbox: at-least-once delivery and dedupe.

Proves the foundation for durable domain events: a message stays deliverable
until acked (so an offline consumer never loses it), and the consumer inbox
turns at-least-once redelivery into once-per-effect on the stable envelope id.
"""

from __future__ import annotations

import pytest

from sandloom.core.eventing import consumer_inbox
from sandloom.core.eventing.consumer_inbox import ConsumerInbox
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.testing import InMemoryCache


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
@pytest.mark.parametrize("bad_id", ["abc", "", "1-", "-1", "1-x"])
async def test_replay_refuses_an_id_that_is_no_entry_id_and_replays_nothing(
    bad_id: str,
) -> None:
    """The Streams transport refuses a malformed id (``InvalidArgument``, which
    the admin API answers with 422); the memory transport does too, whatever its
    DLQ holds, and replays nothing of the same call."""
    t = MemoryTransport(max_deliveries=1)
    await t.ensure_group("s", "g")
    await t.publish("s", _env("poison"))
    await t.consume("s", group="g", consumer="c1")
    assert await t.consume("s", group="g", consumer="c1") == []
    (parked,) = await t.list_dlq("s")

    # The valid id leads the list: the refusal comes before anything is replayed.
    with pytest.raises(InvalidArgument, match="a replay id"):
        await t.replay_dlq("s", ids=[parked.id, bad_id])
    assert t.dlq_depth("s") == 1
    with pytest.raises(InvalidArgument, match="a replay id"):
        await t.replay_dlq("s", ids=[bad_id])
    assert await t.replay_dlq("s", ids=[parked.id]) == 1


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
async def test_consumer_inbox_dedupes_on_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryCache()
    monkeypatch.setattr(consumer_inbox, "cache", lambda name="default": store)
    inbox = ConsumerInbox("emailer")
    assert await inbox.seen("e1") is False  # first time -> process
    assert await inbox.seen("e1") is True  # duplicate -> skip
    assert await inbox.seen("e2") is False  # different id -> process


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [Unavailable("cache down", capability="cache"), RuntimeError("driver bug")],
    ids=["outage", "any_other_error"],
)
async def test_consumer_inbox_fails_open_on_cache_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    class _BrokenCache(InMemoryCache):
        async def add(self, key: str, value: str, *, ttl_seconds: int) -> bool:
            raise failure

    monkeypatch.setattr(consumer_inbox, "cache", lambda name="default": _BrokenCache())
    inbox = ConsumerInbox("emailer")
    # Degrade to at-least-once (process) rather than dropping the event.
    assert await inbox.seen("e1") is False
