"""A security revocation is never silently dead-lettered.

Before: the durable applier of ``core.auth.tokens_revoked.v1`` raised while
the cache was down, and the bus dead-lettered the event after 5 deliveries
(about 15 s of retry backoff) — the revocation was then lost until an
operator replayed the DLQ. Now:

* a consumer registered with ``dead_letter=False`` is retried FOREVER with the
  transport's capped backoff (Streams, in-memory bus);
* while a revocation is stuck, ``auth_revocation_pending_seconds`` reports its
  age, every attempt logs an ERROR, and ``/ready`` answers degraded (503) —
  ``/health`` (liveness) is untouched;
* only an event that can NEVER apply (malformed payload ->
  ``PoisonEventError``) is dead-lettered, at once, with a CRITICAL log and
  ``auth_revocation_malformed_total``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.auth import marks, revocation
from sandloom.core.consumers.callbacks import build_consumer_callback
from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.transport_base import DLQ_REASON_POISON
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import InMemoryCache

CHANNEL = marks.TOKENS_REVOKED_CHANNEL
#: The old delivery budget after which the event used to be dead-lettered.
OLD_BUDGET = 5


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryCache]:
    import sandloom.core.infra.access as access

    fake = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    revocation._stuck_revocations.clear()
    revocation._stuck_marks.clear()
    yield fake
    revocation._stuck_revocations.clear()
    revocation._stuck_marks.clear()


@asynccontextmanager
async def _no_session(_database: str) -> AsyncIterator[None]:
    yield None


async def _applier(session: Any, envelope: dict[str, Any]) -> None:
    """What identity registers (``sessions@tokens_revoked``)."""
    await revocation.apply_pending_revocation(envelope.get("payload") or {})


def _callback(*, dead_letter: bool) -> Any:
    return build_consumer_callback(
        name="identity:sessions@tokens_revoked",
        database="default",
        handler=_applier,
        durable=False,
        opener=_no_session,
        metric_processed="event_consumer_events_processed_total",
        metric_failed="event_consumer_events_failed_total",
        labels={"consumer": "identity:sessions@tokens_revoked"},
        dead_letter=dead_letter,
    )


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "channel": CHANNEL,
        "schema_version": "v1",
        "trace_id": None,
        "emitted_at": "2026-01-01T00:00:00+00:00",
        "payload": payload,
    }


class _OutageCache(InMemoryCache):
    """Down for the first ``failures`` revocation writes (> the old budget)."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def get(self, key: str) -> str | None:
        if self.failures > 0 and key.startswith(marks.SUBJECT_KEY_PREFIX):
            self.failures -= 1
            raise ConnectionError("cache down")
        return await super().get(key)


async def _wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# Streams bus (consumer groups, redelivery, DLQ)
# --------------------------------------------------------------------------- #
async def test_streams_outage_longer_than_the_old_budget_is_applied_not_dlqd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandloom.core.infra.access as access

    failures = OLD_BUDGET * 3  # far past the old dead-letter threshold
    cache = _OutageCache(failures)
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    revocation._stuck_revocations.clear()
    transport = MemoryTransport(
        max_deliveries=OLD_BUDGET, retry_base_ms=1, retry_max_ms=20
    )
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, poll_interval=0.005)
    group = "identity:sessions@tokens_revoked"
    await bus.subscribe(CHANNEL, _callback(dead_letter=False), group=group)
    sub = str(uuid4())
    mark = time.time() + 2
    stuck_seen: list[float] = []
    try:
        await transport.publish(
            CHANNEL, _envelope({"user_id": sub, "valid_after": mark, "reason": "t"})
        )
        await _wait_for(lambda: cache.failures < failures - OLD_BUDGET)
        stuck_seen.append(revocation.pending_revocations()["count"])
        await _wait_for(lambda: marks.subject_key(sub) in cache.store, timeout=20)
        # ... and acked (the ack follows the handler's success)
        await _wait_for(lambda: transport.pending_count(CHANNEL, group) == 0)
    finally:
        await bus.close()
    assert stuck_seen == [1.0]  # reported while it was stuck
    assert transport.dlq_depth(CHANNEL) == 0  # never parked
    assert transport.pending_count(CHANNEL, group) == 0  # acked once applied
    assert revocation.pending_revocations() == {"count": 0.0, "oldest_seconds": 0.0}
    stored = marks._parse_mark_us(cache.store[marks.subject_key(sub)])
    assert stored == marks._seconds_to_us(mark)


async def test_streams_default_consumers_are_still_dead_lettered() -> None:
    """The exemption is per consumer: other groups keep the DLQ policy."""
    transport = MemoryTransport(max_deliveries=2, retry_base_ms=0)
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, poll_interval=0.005)

    async def _always_fails(_env: Any) -> None:
        raise RuntimeError("boom")

    await bus.subscribe("core.test.v1", _always_fails, group="g.default")
    try:
        await transport.publish("core.test.v1", _envelope({}))
        await _wait_for(lambda: transport.dlq_depth("core.test.v1") == 1)
    finally:
        await bus.close()


async def test_streams_malformed_revocation_is_dead_lettered_at_once(
    mem_cache: InMemoryCache, caplog: pytest.LogCaptureFixture
) -> None:
    transport = MemoryTransport(max_deliveries=OLD_BUDGET, retry_base_ms=0)
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport, poll_interval=0.005)
    group = "identity:sessions@tokens_revoked"
    await bus.subscribe(CHANNEL, _callback(dead_letter=False), group=group)
    before = marks.revocation_metrics_snapshot()
    malformed = marks._METRIC_REVOCATION_MALFORMED
    count_before = marks._counter_value(malformed)
    try:
        await transport.publish(
            CHANNEL, _envelope({"user_id": "u", "valid_after": "x"})
        )
        await _wait_for(lambda: transport.dlq_depth(CHANNEL) == 1)
    finally:
        await bus.close()
    (entry,) = await transport.list_dlq(CHANNEL)
    assert entry["attempts"] <= 1  # not retried: it can never apply
    assert marks._counter_value(malformed) == count_before + 1
    assert any(r.levelname == "CRITICAL" for r in caplog.records)
    assert marks.revocation_metrics_snapshot()["applied"] == before["applied"]
    assert DLQ_REASON_POISON == "poison_event"


# --------------------------------------------------------------------------- #
# In-memory bus (dev / tests)
# --------------------------------------------------------------------------- #
async def test_in_memory_bus_retries_a_never_dead_letter_consumer_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandloom.core.infra.access as access

    cache = _OutageCache(OLD_BUDGET * 2)
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    revocation._stuck_revocations.clear()
    bus = InMemoryEventBus(
        InMemorySchemaRegistry(),
        max_deliveries=OLD_BUDGET,
        retry_base_seconds=0.001,
        retry_max_seconds=0.005,
    )
    await bus.subscribe(CHANNEL, _callback(dead_letter=False))
    sub = str(uuid4())
    await bus._publish_to_transport(
        CHANNEL,
        _envelope({"user_id": sub, "valid_after": time.time(), "reason": "t"}),  # type: ignore[arg-type]
    )
    await bus.drain(timeout=10)
    assert bus.dead_letters == []
    assert marks.subject_key(sub) in cache.store
    assert revocation.pending_revocations()["count"] == 0


async def test_in_memory_bus_dead_letters_poison_immediately(
    mem_cache: InMemoryCache,
) -> None:
    bus = InMemoryEventBus(InMemorySchemaRegistry(), max_deliveries=OLD_BUDGET)
    await bus.subscribe(CHANNEL, _callback(dead_letter=False))
    await bus._publish_to_transport(CHANNEL, _envelope({"valid_after": 1.0}))  # type: ignore[arg-type]
    await bus.drain(timeout=5)
    assert [attempts for _, _, attempts in bus.dead_letters] == [1]


# --------------------------------------------------------------------------- #
# Pending gauge, ERROR log, /ready (not /health)
# --------------------------------------------------------------------------- #
async def test_stuck_revocation_is_reported_and_cleared(
    mem_cache: InMemoryCache, caplog: pytest.LogCaptureFixture
) -> None:
    sub = str(uuid4())
    committed_at = time.time() - 30  # the revoking change, 30 s ago
    payload = {"user_id": sub, "valid_after": committed_at + 2.0, "reason": "t"}
    mem_cache.broken = True
    with pytest.raises(marks.RevocationUnavailableError):
        await revocation.apply_pending_revocation(payload)
    pending = revocation.pending_revocations()
    assert pending["count"] == 1
    assert 29 <= pending["oldest_seconds"] <= 40
    gauge = marks._GAUGE_REVOCATION_PENDING.collect()[0].samples[0].value
    assert gauge == pytest.approx(pending["oldest_seconds"], abs=1)
    assert any(
        r.levelname == "ERROR" and "still pending" in r.getMessage()
        for r in caplog.records
    )

    mem_cache.broken = False
    await revocation.apply_pending_revocation(payload)
    assert revocation.pending_revocations() == {"count": 0.0, "oldest_seconds": 0.0}


async def test_stuck_entry_clears_when_another_worker_applied_the_delivery(
    mem_cache: InMemoryCache,
) -> None:
    # Review hand-off: this worker failed to apply the revocation, then a
    # Streams reclaim handed its pending delivery to ANOTHER worker, which
    # applied and acked it. This worker never sees the delivery again, so its
    # per-process entry used to keep /ready degraded forever.
    sub = str(uuid4())
    payload = {"user_id": sub, "valid_after": time.time() - 5, "reason": "t"}
    mem_cache.broken = True
    with pytest.raises(marks.RevocationUnavailableError):
        await revocation.apply_pending_revocation(payload)
    await revocation.reconcile_pending_revocations()  # cache still down: kept
    assert revocation.pending_revocations()["count"] == 1

    mem_cache.broken = False
    await revocation.reconcile_pending_revocations()  # mark not written: kept
    assert revocation.pending_revocations()["count"] == 1

    # "another worker" applies the same delivery (writes the mark)
    assert await revocation.revoke_user_tokens(sub, valid_after=payload["valid_after"])
    await revocation.reconcile_pending_revocations()
    assert revocation.pending_revocations() == {"count": 0.0, "oldest_seconds": 0.0}
    assert revocation._stuck_marks == {}


async def test_ready_degrades_while_a_revocation_is_stuck_liveness_unaffected(
    mem_cache: InMemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from sandloom.core.app_factory import _install_ops_endpoints
    from sandloom.core.config.settings import settings
    from sandloom.core.facade import CoreFacade

    application = FastAPI()
    core = CoreFacade(app=application)
    _install_ops_endpoints(application, core)
    token = "ready-revocation-test-internal-token-0123456789"
    monkeypatch.setattr(settings, "internal_api_token", token)
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    headers = {"X-Internal-Token": token}
    sub = str(uuid4())
    payload = {"user_id": sub, "valid_after": time.time(), "reason": "t"}

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        healthy = await client.get("/ready", headers=headers)
        mem_cache.broken = True
        with pytest.raises(marks.RevocationUnavailableError):
            await revocation.apply_pending_revocation(payload)
        stuck = await client.get("/ready", headers=headers)
        live = await client.get("/health/simple")
        mem_cache.broken = False
        await revocation.apply_pending_revocation(payload)
        recovered = await client.get("/ready", headers=headers)

    assert healthy.json()["revocations_pending"] is False
    assert stuck.status_code == 503
    assert stuck.json()["status"] == "degraded"
    assert stuck.json()["revocations_pending"] is True
    assert stuck.json()["revocations_pending_count"] == 1
    assert live.status_code == 200  # liveness never depends on it
    assert recovered.json()["revocations_pending"] is False
    assert recovered.status_code == healthy.status_code


# --------------------------------------------------------------------------- #
# Real Redis Streams
# --------------------------------------------------------------------------- #
async def test_redis_streams_never_dead_letter_group_survives_the_old_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_asyncio = pytest.importorskip("redis.asyncio")
    from sandloom.core.eventing.streams_transport import RedisStreamsTransport

    url = os.getenv("SANDLOOM_TEST_CACHE_URL") or os.getenv("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not set")
    client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    stream = f"test.revocation.{uuid4().hex[:8]}.v1"

    async def _factory() -> Any:
        return client

    transport = RedisStreamsTransport(
        client_factory=_factory,
        max_deliveries=OLD_BUDGET,
        retry_base_ms=1,
        retry_max_ms=5,
    )
    group = "identity:sessions@tokens_revoked"
    transport.never_dead_letter(stream, group)
    await transport.ensure_group(stream, group)
    try:
        await transport.publish(stream, _envelope({"n": 1}))  # type: ignore[arg-type]
        attempts = 0
        consumer = "c1"
        while attempts < OLD_BUDGET * 3:  # the handler keeps failing
            for _ in await transport.consume(stream, group=group, consumer=consumer):
                attempts += 1
            await asyncio.sleep(0.01)
        assert await transport.list_dlq(stream) == []
        delivered: list[Any] = []
        deadline = time.monotonic() + 5
        while not delivered and time.monotonic() < deadline:
            delivered = await transport.consume(stream, group=group, consumer=consumer)
            await asyncio.sleep(0.01)
        assert len(delivered) == 1  # still deliverable after 15 failures
        await transport.ack(delivered[0])
        assert await transport.list_dlq(stream) == []

        # ... and an explicit poison still reaches the DLQ.
        await transport.publish(stream, _envelope({"n": 2}))  # type: ignore[arg-type]
        (poison,) = await transport.consume(stream, group=group, consumer=consumer)
        await transport.dead_letter(poison, reason=DLQ_REASON_POISON)
        (parked,) = await transport.list_dlq(stream)
        assert parked["reason"] == DLQ_REASON_POISON
    finally:
        await client.delete(stream, f"{stream}.dlq")
        await client.aclose()


def test_poison_error_is_public() -> None:
    from sandloom.core import di

    assert di.PoisonEventError is PoisonEventError
