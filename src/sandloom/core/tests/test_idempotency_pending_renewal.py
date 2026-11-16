"""A ``pending`` idempotency reservation lives as long as its request (GLM-4).

The pending TTL is short (5 min) so a reservation whose request DIED self-heals
quickly. But a request still RUNNING past it used to lose its reservation: a
retry then reserved the key and executed concurrently (HTTP guard, the
cache-fallback command dedupe, legacy RPC). The reservation is now renewed
while the task that made it is alive — and only then, so the self-heal of a
dead request is unchanged. The expiry is the store's: the in-memory store models
TTLs against the monotonic clock, as the Redis server does.
"""

from __future__ import annotations

import asyncio

import pytest

from sandloom.core import idempotency
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.idempotency import reservation as idempotency_reservation
from sandloom.core.testing import InMemoryAtomicStore

KEY = "idem:test:renewal"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> InMemoryAtomicStore:
    store = InMemoryAtomicStore()
    monkeypatch.setattr(idempotency_cache_store, "atomic_store", lambda role: store)
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RENEW_INTERVAL_SECONDS", 0.2)
    return store


@pytest.mark.asyncio
async def test_a_running_request_keeps_its_reservation_past_the_pending_ttl(
    provider: InMemoryAtomicStore,
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
    provider: InMemoryAtomicStore,
) -> None:
    """The owner task ended without a terminal record (cancelled, leaked):
    renewal stops and the key frees at the pending TTL, as before."""

    async def _dies_after_reserving() -> None:
        outcome, _ = await idempotency.reserve_key(KEY, "fp", 3600)
        assert outcome == "reserved"

    await asyncio.create_task(_dies_after_reserving())
    # Until the server expires the key (1 s pending TTL), not a fixed 1.8 s.
    for _ in range(300):
        if await provider.get(KEY) is None:
            break
        await asyncio.sleep(0.02)
    outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)


@pytest.mark.asyncio
async def test_a_released_reservation_is_no_longer_renewed(
    provider: InMemoryAtomicStore,
) -> None:
    outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)
    assert not idempotency_reservation._live_reservations
    assert await provider.get(KEY) is None
