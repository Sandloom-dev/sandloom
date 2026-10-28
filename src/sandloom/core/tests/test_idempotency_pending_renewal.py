"""A ``pending`` idempotency reservation lives as long as its request (GLM-4).

The pending TTL is short (5 min) so a reservation whose request DIED self-heals
quickly. But a request still RUNNING past it used to lose its reservation: a
retry then reserved the key and executed concurrently (HTTP guard, the
cache-fallback command dedupe, legacy RPC). The reservation is now renewed
while the task that made it is alive — and only then, so the self-heal of a
dead request is unchanged. Real Redis: the expiry is the server's.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from sandloom.core import idempotency
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.idempotency import reservation as idempotency_reservation
from sandloom.core.testing import reload_test_env

redis_asyncio = pytest.importorskip("redis.asyncio")

KEY = "idem:test:renewal"


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture
async def provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not set")
    cache = RedisCacheProvider(
        name="idem-renew-test", driver="redis", options={"url": url}, shared=False
    )
    try:
        client = await cache.raw_client()
        await client.ping()
    except (OSError, redis_asyncio.RedisError):  # pragma: no cover
        pytest.skip("Redis unreachable")
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda: cache)
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RENEW_INTERVAL_SECONDS", 0.2)
    await client.delete(KEY)
    yield cache
    await client.delete(KEY)
    await cache.aclose()


@pytest.mark.asyncio
async def test_a_running_request_keeps_its_reservation_past_the_pending_ttl(
    provider: RedisCacheProvider,
) -> None:
    reserved = asyncio.Event()
    finish = asyncio.Event()

    async def _long_request() -> None:
        outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
        assert outcome == "reserved"
        assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
        reserved.set()
        await finish.wait()  # the endpoint runs longer than the pending TTL
        record = {"state": "succeeded", "fp": "fp", "response": {"status": 200}}
        assert (
            await idempotency_reservation._persist_terminal(reservation, record) is True
        )

    owner = asyncio.create_task(_long_request())
    await reserved.wait()
    await asyncio.sleep(1.8)  # well past the 1 s pending TTL
    outcome, _ = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "in_progress"  # never a second, concurrent execution
    finish.set()
    await owner
    outcome, stored = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "replay"
    assert stored == {"status": 200}


@pytest.mark.asyncio
async def test_a_dead_requests_reservation_still_self_heals(
    provider: RedisCacheProvider,
) -> None:
    """The owner task ended without a terminal record (cancelled, leaked):
    renewal stops and the key frees at the pending TTL, as before."""

    async def _dies_after_reserving() -> None:
        outcome, _ = await idempotency.reserve_key(KEY, "fp", 3600)
        assert outcome == "reserved"

    await asyncio.create_task(_dies_after_reserving())
    await asyncio.sleep(1.8)
    outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)


@pytest.mark.asyncio
async def test_a_released_reservation_is_no_longer_renewed(
    provider: RedisCacheProvider,
) -> None:
    outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)
    assert not idempotency_reservation._live_reservations
    assert await provider.get(KEY) is None
