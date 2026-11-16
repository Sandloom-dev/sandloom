"""The compare-and-set primitives of the ``redis`` atomic store on a real Redis:
the idempotency guard's owner checks rest on them (the guard's own transitions,
on the kernel's in-memory store, are in
``src/sandloom/core/tests/test_idempotency_cas.py``)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _redis_required import redis_unavailable
from sandloom_redis.atomic_store import RedisAtomicStoreProvider

from sandloom.core.testing import reload_test_env

KEY = "idem:test:k1"


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture
async def provider() -> AsyncIterator[RedisAtomicStoreProvider]:
    url = os.getenv("CACHE_URL")
    if not url:
        redis_unavailable("CACHE_URL not set")
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
