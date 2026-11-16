"""The compare-and-set primitives of the ``redis`` atomic store on a real Redis:
the idempotency guard's owner checks rest on them (the guard's own transitions,
on the kernel's in-memory store, are in
``src/sandloom/core/tests/test_idempotency_cas.py``), and the pending
reservation's renewal and expiry, which the server times."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _redis_required import cache_url_or_unavailable, redis_unavailable
from sandloom_redis.atomic_store import RedisAtomicStoreProvider

from sandloom.core import idempotency
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import reservation as idempotency_reservation
from sandloom.core.idempotency.fingerprint import IdempotencyReservation
from sandloom.core.testing import reload_test_env

KEY = "idem:test:k1"
PENDING_KEY = "idem:test:renewal"
DEAD_KEY = "idem:test:renewal-dead"


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture
async def provider() -> AsyncIterator[RedisAtomicStoreProvider]:
    url = cache_url_or_unavailable()
    cache = RedisAtomicStoreProvider(
        name="idem-test", driver="redis", options={"url": url}
    )
    try:
        client = await cache._client_obj()
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
        redis_unavailable("Redis unreachable")
    yield cache
    for key in await client.keys("idem:test:*"):
        await client.delete(key)
    await cache.aclose()


@pytest.mark.asyncio
async def test_redis_cas_primitives(provider: RedisAtomicStoreProvider) -> None:
    await provider.set(KEY, "v1")
    assert await provider.compare_and_set(KEY, "other", "v2") is False
    assert await provider.compare_and_set(KEY, "v1", "v2", ttl_seconds=60) is True
    assert await provider.get(KEY) == "v2"
    assert await provider.compare_and_delete(KEY, "v1") is False
    assert await provider.compare_and_delete(KEY, "v2") is True
    assert await provider.get(KEY) is None


@pytest.fixture
def short_pending_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(idempotency_reservation, "_PENDING_RENEW_INTERVAL_SECONDS", 0.2)


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_pending_reservation")
async def test_the_pending_reservation_is_renewed_and_expires_on_the_redis_server(
    provider: RedisAtomicStoreProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-IDEM-02 on a real Redis, where the expiry is the server's: a running
    request keeps its reservation past the 1 s pending TTL (renewal), and the
    reservation of a request that died frees at the server's TTL. The kernel's
    ``test_idempotency_pending_renewal.py`` runs the same transitions on the
    in-memory store."""
    monkeypatch.setattr(idempotency_cache_store, "atomic_store", lambda role: provider)
    reserved = asyncio.Event()
    finish = asyncio.Event()

    async def _long_request() -> None:
        outcome, reservation = await idempotency.reserve_key(PENDING_KEY, "fp", 3600)
        assert outcome == "reserved"
        assert isinstance(reservation, IdempotencyReservation)
        reserved.set()
        await finish.wait()
        record = {"state": "succeeded", "fp": "fp", "response": {"status": 200}}
        assert (
            await idempotency_reservation._persist_terminal(reservation, record) is True
        )

    owner = asyncio.create_task(_long_request())
    await reserved.wait()
    await asyncio.sleep(1.8)  # well past the server's 1 s TTL
    outcome, _ = await idempotency.reserve_key(PENDING_KEY, "fp", 3600)
    assert outcome == "in_progress"  # renewed: never a second, concurrent execution
    finish.set()
    await owner
    outcome, stored = await idempotency.reserve_key(PENDING_KEY, "fp", 3600)
    assert (outcome, stored) == ("replay", {"status": 200})

    async def _dies_after_reserving() -> None:
        outcome, _ = await idempotency.reserve_key(DEAD_KEY, "fp", 3600)
        assert outcome == "reserved"

    await asyncio.create_task(_dies_after_reserving())
    for _ in range(300):  # until the server expires the key
        if await provider.get(DEAD_KEY) is None:
            break
        await asyncio.sleep(0.02)
    outcome, reservation = await idempotency.reserve_key(DEAD_KEY, "fp", 3600)
    assert outcome == "reserved"
    assert isinstance(reservation, IdempotencyReservation)
    await idempotency_reservation._release_key(reservation)
