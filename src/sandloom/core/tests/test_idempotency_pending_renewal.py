"""A ``pending`` idempotency reservation lives as long as its request (GLM-4).

The pending TTL is short (5 min) so a reservation whose request DIED self-heals
quickly. But a request still RUNNING past it used to lose its reservation: a
retry then reserved the key and executed concurrently (HTTP guard, the
cache-fallback command dedupe, legacy RPC). The reservation is now renewed
while the task that made it is alive — and only then, so the self-heal of a
dead request is unchanged. The expiry is the store's: the in-memory store
models TTLs against a clock, as the Redis server does, and here that clock is
one the test moves (no test waits out a TTL).
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


class _MovableStore(InMemoryAtomicStore):
    """The in-memory store on a clock only the test moves (``now``), which
    signals every successful compare-and-set (the keeper's renewal)."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0
        self.clock = lambda: self.now
        self.renewed = asyncio.Event()

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        swapped = await super().compare_and_set(
            key, expected, value, ttl_seconds=ttl_seconds
        )
        if swapped:
            self.renewed.set()
        return swapped


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _MovableStore:
    store = _MovableStore()
    monkeypatch.setattr(idempotency_cache_store, "atomic_store", lambda role: store)
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RESERVATION_TTL_SECONDS", 1)
    # The keeper's cadence is real time; the store's expiry is the test's clock.
    monkeypatch.setattr(
        idempotency_reservation, "_PENDING_RENEW_INTERVAL_SECONDS", 0.01
    )
    return store


@pytest.mark.asyncio
async def test_a_running_request_keeps_its_reservation_past_the_pending_ttl(
    provider: _MovableStore,
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
    # The store's clock passes 3.2 pending TTLs (1 s each) in steps of 0.4 s.
    # Each step waits for the keeper's next renewal before the clock moves on,
    # so the key is never more than 0.4 s past its last renewal, however slow
    # the machine is: it can only still be there because it was renewed.
    for _ in range(8):
        provider.renewed.clear()
        provider.now += 0.4
        await asyncio.wait_for(provider.renewed.wait(), timeout=3)
    outcome, _ = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "in_progress"  # never a second, concurrent execution
    finish.set()
    await owner
    outcome, stored = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "replay"
    assert stored == {"status": 200}


@pytest.mark.asyncio
async def test_a_dead_requests_reservation_still_self_heals(
    provider: _MovableStore,
) -> None:
    """The owner task ended without a terminal record (cancelled, leaked):
    renewal stops and the key frees at the pending TTL, as before."""

    async def _dies_after_reserving() -> None:
        outcome, _ = await idempotency.reserve_key(KEY, "fp", 3600)
        assert outcome == "reserved"

    await asyncio.create_task(_dies_after_reserving())
    provider.now += 0.9
    assert await provider.get(KEY) is not None  # inside the 1 s pending TTL
    provider.now += 0.2
    assert await provider.get(KEY) is None  # the store expired it at the TTL
    outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)


@pytest.mark.asyncio
async def test_a_released_reservation_is_no_longer_renewed(
    provider: _MovableStore,
) -> None:
    outcome, reservation = await idempotency.reserve_key(KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, idempotency_fingerprint.IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)
    assert not idempotency_reservation._live_reservations
    assert await provider.get(KEY) is None
