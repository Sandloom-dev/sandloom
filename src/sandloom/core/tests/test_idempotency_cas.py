"""Idempotency key transitions are owner-checked (M3), against real Redis.

A request whose ``pending`` reservation expired must never delete or overwrite
the reservation a retry made afterwards (that would let a THIRD request run
concurrently — double execution). A response stream aborted after a success
status must still leave a terminal record (else a retry re-executes once the
pending TTL lapses).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import orjson
import pytest

from sandloom.core import idempotency
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import capture as idempotency_capture
from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.idempotency import replay as idempotency_replay
from sandloom.core.idempotency import reservation as idempotency_reservation
from sandloom.core.testing import reload_test_env

redis_asyncio = pytest.importorskip("redis.asyncio")


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture
async def provider(monkeypatch: pytest.MonkeyPatch):
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not set")
    cache = RedisCacheProvider(
        name="idem-test", driver="redis", options={"url": url}, shared=False
    )
    try:
        client = await cache.raw_client()
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda: cache)
    yield cache
    for key in await client.keys("idem:test:*"):
        await client.delete(key)
    await cache.aclose()


KEY = "idem:test:k1"


@pytest.mark.asyncio
async def test_redis_cas_primitives(provider: RedisCacheProvider) -> None:
    await provider.set(KEY, "v1")
    assert await provider.compare_and_set(KEY, "other", "v2") is False
    assert await provider.compare_and_set(KEY, "v1", "v2", ttl_seconds=60) is True
    assert await provider.get(KEY) == "v2"
    assert await provider.compare_and_delete(KEY, "v1") is False
    assert await provider.compare_and_delete(KEY, "v2") is True
    assert await provider.get(KEY) is None


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_a_retrys_reservation(
    provider: RedisCacheProvider,
) -> None:
    outcome_a, res_a = await idempotency.reserve_key(KEY, "fp", 60)
    assert outcome_a == "reserved"
    await provider.delete(KEY)  # A's short pending reservation expires
    outcome_b, res_b = await idempotency.reserve_key(KEY, "fp", 60)
    assert outcome_b == "reserved"

    # A fails late and releases: B's reservation must survive ...
    await idempotency_reservation._release_key(res_a)  # type: ignore[arg-type]
    assert await provider.get(KEY) == res_b.pending_value  # type: ignore[union-attr]
    # ... so a third request C still sees "in progress" (no concurrent run).
    outcome_c, _ = await idempotency.reserve_key(KEY, "fp", 60)
    assert outcome_c == "in_progress"


@pytest.mark.asyncio
async def test_expired_owner_cannot_finalise_over_a_retrys_reservation(
    provider: RedisCacheProvider,
) -> None:
    _, res_a = await idempotency.reserve_key(KEY, "fp", 60)
    await provider.delete(KEY)
    _, res_b = await idempotency.reserve_key(KEY, "fp", 60)

    record = idempotency_replay.success_record(
        res_a,  # type: ignore[arg-type]
        status_code=200,
        body=b"{}",
        headers={},
        media_type="application/json",
    )
    assert await idempotency_reservation._persist_terminal(res_a, record) is False  # type: ignore[arg-type]
    assert await provider.get(KEY) == res_b.pending_value  # type: ignore[union-attr]

    # B finalises its own reservation normally.
    assert await idempotency_reservation._persist_terminal(res_b, record) is True  # type: ignore[arg-type]
    outcome, stored = await idempotency.reserve_key(KEY, "fp", 60)
    assert outcome == "replay" and stored["status"] == 200  # type: ignore[index]


@pytest.mark.asyncio
async def test_success_is_recorded_even_if_reservation_expired_unclaimed(
    provider: RedisCacheProvider,
) -> None:
    _, res_a = await idempotency.reserve_key(KEY, "fp", 60)
    await provider.delete(KEY)  # expired; nobody retried yet
    record = idempotency_replay.success_record(
        res_a,  # type: ignore[arg-type]
        status_code=201,
        body=b"ok",
        headers={},
        media_type=None,
    )
    assert await idempotency_reservation._persist_terminal(res_a, record) is True  # type: ignore[arg-type]
    outcome, _ = await idempotency.reserve_key(KEY, "fp", 60)
    assert outcome == "replay"  # a later retry replays, never re-executes


@pytest.mark.asyncio
async def test_aborted_success_stream_still_writes_terminal_record(
    provider: RedisCacheProvider,
) -> None:
    _, res = await idempotency.reserve_key(KEY, "fp", 60)

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        # The guard armed this request (fresh reservation) before the endpoint.
        scope["state"][idempotency_fingerprint._ARMED_ATTR] = res
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part-1", "more_body": True})
        raise ConnectionResetError("client disconnected mid-stream")

    async def _send(_message: Any) -> None:
        return None

    async def _receive() -> Any:  # pragma: no cover - never read
        return {"type": "http.request", "body": b""}

    middleware = idempotency.IdempotencyCaptureMiddleware(_app)
    with pytest.raises(ConnectionResetError):  # propagates unwrapped (pure ASGI)
        await middleware({"type": "http", "state": {}}, _receive, _send)
    await asyncio.gather(*list(idempotency_capture._background_persists))

    raw = await provider.get(KEY)
    assert raw is not None
    stored = orjson.loads(raw)
    assert stored["state"] == "succeeded"
    assert stored["response"]["truncated"] is True  # incomplete body not replayed
