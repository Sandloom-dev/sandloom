"""StreamsEventBus: durable at-least-once delivery over a MessageTransport.

Uses MemoryTransport + a patched in-memory cache (for the dedupe inbox) so the
test is hermetic and deterministic. Proves: a subscriber receives published
events, an event published before the subscriber existed is still delivered
(durability), a handler that raises leaves the message for redelivery, and the
same event id is not delivered twice.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sandloom.core.eventing.consumer_inbox import ConsumerInbox
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.subscriptions import SubscriptionHandle, SubscriptionState
from sandloom.core.tests._schemas import PermissiveSchemaRegistry


@pytest.fixture(autouse=True)
def _fake_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}

    class _FakeRedis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return None
            store[key] = value
            return True

        async def get(self, key):
            return store.get(key)

    async def _get_cache():
        return _FakeRedis()

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", _get_cache
    )


def _bus(transport: MemoryTransport) -> StreamsEventBus:
    return StreamsEventBus(PermissiveSchemaRegistry(), transport, poll_interval=0.01)


@pytest.mark.asyncio
async def test_cancel_mid_batch_acks_prior_successes() -> None:
    transport = MemoryTransport()
    bus = _bus(transport)
    channel, group = "core.test.v1", "g.cancel"
    blocked = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        if envelope["id"] == "second":
            blocked.set()
            await asyncio.Event().wait()

    bus._subs[(channel, group)] = [handler]
    await transport.ensure_group(channel, group)
    for event_id in ("first", "second"):
        await transport.publish(
            channel,
            EventEnvelope(
                id=event_id,
                channel=channel,
                schema_version="v1",
                trace_id=None,
                emitted_at="2026-01-01T00:00:00+00:00",
                payload={},
            ),
        )
    batch = await transport.consume(channel, group=group, consumer="c1")
    dispatch = asyncio.create_task(
        bus._dispatch_batch(channel, group, batch, ConsumerInbox("cancel-test"))
    )
    await asyncio.wait_for(blocked.wait(), timeout=1)
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    pending = await transport.consume(channel, group=group, consumer="c1")
    assert [item.envelope["id"] for item in pending] == ["second"]


@pytest.mark.asyncio
async def test_handler_timeout_leaves_delivery_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "event_handler_timeout_seconds", 0.01)
    started = asyncio.Event()

    async def handler(_envelope: EventEnvelope) -> None:
        started.set()
        await asyncio.Event().wait()

    result = await StreamsEventBus._run_callbacks_outcome(
        "core.test.v1",
        [handler],
        EventEnvelope(
            id="timeout",
            channel="core.test.v1",
            schema_version="v1",
            trace_id=None,
            emitted_at="2026-01-01T00:00:00+00:00",
            payload={},
        ),
    )
    assert started.is_set()
    assert result == "failed"


@pytest.mark.asyncio
async def test_stalled_handle_degrades_without_failing_idle_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.eventing import subscriptions

    clock = [100.0]
    monkeypatch.setattr(subscriptions.time, "monotonic", lambda: clock[0])

    async def close() -> None:
        return None

    handle = SubscriptionHandle(close)
    handle.mark_running()
    clock[0] += 1000
    assert handle.state is SubscriptionState.RUNNING
    handle.mark_processing(30)
    clock[0] += 31
    assert handle.state is SubscriptionState.RETRYING
    assert "deadline" in (handle.snapshot().last_error or "")
    handle.mark_progress()
    assert handle.state is SubscriptionState.RUNNING


def test_reconnect_backoff_is_capped_and_jittered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.eventing import streams_bus

    monkeypatch.setattr(
        streams_bus.settings, "event_streams_reconnect_base_seconds", 1.0
    )
    monkeypatch.setattr(
        streams_bus.settings, "event_streams_reconnect_max_seconds", 8.0
    )
    monkeypatch.setattr(streams_bus.random, "uniform", lambda a, b: (a + b) / 2)
    assert [StreamsEventBus._reconnect_delay(n) for n in range(1, 7)] == [
        0.75,
        1.5,
        3.0,
        6.0,
        6.0,
        6.0,
    ]


@pytest.mark.asyncio
async def test_subscription_reports_transport_loss_and_recovery() -> None:
    failed = asyncio.Event()

    class _FlakyTransport(MemoryTransport):
        async def consume(self, *args, **kwargs):
            if not failed.is_set():
                failed.set()
                raise ConnectionError("redis unavailable")
            return await super().consume(*args, **kwargs)

    bus = _bus(_FlakyTransport())

    async def handler(_env) -> None:
        return None

    handle = await bus.subscribe("core.test.v1", handler, group="g.health")
    await asyncio.wait_for(failed.wait(), timeout=1)

    assert handle.state is SubscriptionState.RETRYING
    await handle.wait_ready(timeout=1)
    assert handle.state is SubscriptionState.RUNNING
    await bus.close()


@pytest.mark.asyncio
async def test_unsubscribe_stops_only_its_handle_within_shared_group() -> None:
    bus = _bus(MemoryTransport())

    async def first(_env) -> None:
        return None

    async def second(_env) -> None:
        return None

    first_handle = await bus.subscribe("core.test.v1", first, group="g.shared")
    second_handle = await bus.subscribe("core.test.v1", second, group="g.shared")

    bus.unsubscribe("core.test.v1", first)
    await asyncio.sleep(0)

    assert first_handle.state is SubscriptionState.STOPPED
    assert second_handle.state is SubscriptionState.RUNNING
    await bus.close()


@pytest.mark.asyncio
async def test_subscriber_receives_published_event() -> None:
    bus = _bus(MemoryTransport())
    got: list[str] = []
    fired = asyncio.Event()

    async def handler(env) -> None:
        got.append(env["id"])
        fired.set()

    await bus.subscribe("core.test.v1", handler)
    await bus.publish_event("core.test.v1", {"n": 1})
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert len(got) == 1
    await bus.close()


@pytest.mark.asyncio
async def test_event_published_before_subscribe_is_delivered() -> None:
    transport = MemoryTransport()
    # Ensure the group exists, publish "while offline", then subscribe.
    await transport.ensure_group("core.test.v1", "g.test")
    bus = _bus(transport)
    got: list[str] = []
    fired = asyncio.Event()

    async def handler(env) -> None:
        got.append(env["id"])
        fired.set()

    await bus.publish_event("core.test.v1", {"n": 1})
    await bus.subscribe("core.test.v1", handler, group="g.test")
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert len(got) == 1
    await bus.close()


@pytest.mark.asyncio
async def test_failed_handler_leaves_message_for_redelivery() -> None:
    transport = MemoryTransport()
    bus = _bus(transport)
    attempts: list[str] = []
    succeeded = asyncio.Event()
    fail_first = {"do": True}

    async def handler(env) -> None:
        attempts.append(env["id"])
        if fail_first["do"]:
            fail_first["do"] = False
            raise RuntimeError("transient")
        succeeded.set()

    await bus.subscribe("core.test.v1", handler, group="g.r")
    await bus.publish_event("core.test.v1", {"n": 1})
    # First attempt raises (unacked -> redelivered), second succeeds.
    await asyncio.wait_for(succeeded.wait(), timeout=2.0)
    assert len(attempts) >= 2
    await bus.close()


@pytest.mark.asyncio
async def test_wildcard_subscription_rejected() -> None:
    bus = _bus(MemoryTransport())
    with pytest.raises(ValueError, match="wildcard"):
        await bus.subscribe("core.*", lambda _env: None)  # type: ignore[arg-type,return-value]
    await bus.close()


@pytest.mark.asyncio
async def test_durable_consumer_dedupes_redelivery_without_cache_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group of durable callbacks never touches the cache inbox: a
    redelivered event id is dropped by the DB claim (here an in-memory one)."""
    from contextlib import asynccontextmanager

    from sandloom.core.consumers.callbacks import build_consumer_callback
    from sandloom.core.eventing import consumer_inbox as transport_mod
    from sandloom.core.sql_alchemy import durable_inbox

    claimed: set[tuple[str, str]] = set()

    class _Claims:
        def __init__(self, consumer: str) -> None:
            self._consumer = consumer

        async def claim(self, _session: Any, event_id: str) -> bool:
            key = (self._consumer, event_id)
            if key in claimed:
                return False
            claimed.add(key)
            return True

    cache_calls: list[Any] = []

    def _no_cache(*args: Any, **_k: Any) -> Any:
        cache_calls.append(args)  # the inbox fails open: record, then raise
        raise RuntimeError("durable consumers must not use the cache inbox")

    monkeypatch.setattr(durable_inbox, "DurableConsumerInbox", _Claims)
    monkeypatch.setattr(transport_mod, "cache", _no_cache)

    @asynccontextmanager
    async def opener(_database: str) -> Any:
        yield object()

    applied: list[str] = []

    async def handler(_session: Any, envelope: EventEnvelope) -> None:
        applied.append(envelope["id"])

    callback = build_consumer_callback(
        name="c.durable",
        database="default",
        handler=handler,
        durable=True,
        opener=opener,
        metric_processed="p",
        metric_failed="f",
        labels={"consumer": "c.durable"},
    )
    transport = MemoryTransport()
    bus = _bus(transport)
    channel, group = "core.test.v1", "g.durable"
    bus._subs[(channel, group)] = [callback]
    await transport.ensure_group(channel, group)
    envelope = EventEnvelope(
        id="same",
        channel=channel,
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={},
    )
    for _ in range(2):  # the relay redelivered the same event id
        await transport.publish(channel, envelope)
    inbox = ConsumerInbox("durable-test")
    for _ in range(2):
        batch = await transport.consume(channel, group=group, consumer="c1")
        await bus._dispatch_batch(channel, group, batch, inbox)

    assert applied == ["same"]
    assert transport.pending_count(channel, group) == 0
    assert cache_calls == []


class _LeasedTransport(MemoryTransport):
    """MemoryTransport with the Redis transport's lease surface."""

    renew_interval_seconds = 0.02

    def __init__(self) -> None:
        super().__init__()
        self.renewals: list[list[str]] = []
        self.lose_lease = False

    async def renew_pending(self, deliveries: Any, consumer: str) -> None:
        self.renewals.append([d.id for d in deliveries])
        if self.lose_lease:
            raise RuntimeError("Streams delivery lease lost; aborting active handler")


async def _leased_batch(
    transport: _LeasedTransport, handler: Any, *, n: int = 1
) -> tuple[StreamsEventBus, list[Any]]:
    bus = _bus(transport)
    channel, group = "core.test.v1", "g.lease"
    bus._subs[(channel, group)] = [handler]
    await transport.ensure_group(channel, group)
    for i in range(n):
        await transport.publish(
            channel,
            EventEnvelope(
                id=f"e{i}",
                channel=channel,
                schema_version="v1",
                trace_id=None,
                emitted_at="2026-01-01T00:00:00+00:00",
                payload={},
            ),
        )
    return bus, await transport.consume(channel, group=group, consumer="c1")


async def _dispatch_leased(bus: StreamsEventBus, batch: list[Any]) -> None:
    lease = bus._batch_lease("core.test.v1", "g.lease", "c1")
    await bus._dispatch_with_lease(
        "core.test.v1", "g.lease", batch, ConsumerInbox("lease-test"), "c1", lease
    )


@pytest.mark.asyncio
async def test_fast_batch_runs_inline_without_a_renewal() -> None:
    transport = _LeasedTransport()
    bus, batch = await _leased_batch(transport, _noop_handler, n=3)
    tasks_before = len(asyncio.all_tasks())

    await _dispatch_leased(bus, batch)

    assert transport.renewals == []
    assert len(asyncio.all_tasks()) == tasks_before
    assert transport.pending_count("core.test.v1", "g.lease") == 0


async def _noop_handler(_envelope: EventEnvelope) -> None:
    return None


@pytest.mark.asyncio
async def test_long_handler_is_renewed_until_the_batch_ends() -> None:
    transport = _LeasedTransport()
    seen: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope["id"])
        if envelope["id"] == "e1":
            await asyncio.sleep(0.15)

    bus, batch = await _leased_batch(transport, handler, n=2)
    await _dispatch_leased(bus, batch)

    assert seen == ["e0", "e1"]
    assert len(transport.renewals) >= 2
    # Every renewal covers exactly the batch's still-unacked entries.
    assert all(batch[1].id in ids for ids in transport.renewals)
    assert all(set(ids) <= {d.id for d in batch} for ids in transport.renewals)
    renewed = len(transport.renewals)
    await asyncio.sleep(0.06)
    assert len(transport.renewals) == renewed  # no timer outlives the batch


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow", [False, True])
async def test_lost_lease_cancels_the_active_handler_and_raises(swallow: bool) -> None:
    transport = _LeasedTransport()
    transport.lose_lease = True
    cancelled = asyncio.Event()

    async def handler(_envelope: EventEnvelope) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            if not swallow:
                raise

    bus, batch = await _leased_batch(transport, handler)
    with pytest.raises(RuntimeError, match="lease lost"):
        await asyncio.wait_for(_dispatch_leased(bus, batch), timeout=2)

    assert cancelled.is_set()
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0  # the loop keeps running
    if not swallow:  # neither acked nor marked: redelivered to a successor
        assert transport.pending_count("core.test.v1", "g.lease") == 1


@pytest.mark.asyncio
async def test_stop_during_lost_lease_still_cancels_the_consume_loop() -> None:
    transport = _LeasedTransport()
    started = asyncio.Event()

    async def handler(_envelope: EventEnvelope) -> None:
        started.set()
        await asyncio.sleep(10)

    bus, batch = await _leased_batch(transport, handler)
    loop_task = asyncio.create_task(_dispatch_leased(bus, batch))
    await asyncio.wait_for(started.wait(), timeout=1)
    transport.lose_lease = True
    loop_task.cancel()  # shutdown races the lease loss
    await asyncio.sleep(0.05)
    with pytest.raises(asyncio.CancelledError):
        await loop_task
