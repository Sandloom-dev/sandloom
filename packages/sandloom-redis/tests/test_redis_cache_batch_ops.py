"""Batch cache ops (get_many / set_many) of the ``redis`` cache driver. The
single-key defaults of the kernel's in-memory cache are tested in
``src/sandloom/core/tests/test_cache_batch_ops.py``."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from _redis_required import redis_unavailable
from sandloom_redis.cache import RedisCacheProvider

from sandloom.core.testing.workers import worker_env_values


@pytest.mark.asyncio
async def test_redis_batch_ops_round_trip() -> None:
    env = worker_env_values(Path(__file__).resolve().parents[3] / ".env.test")
    url = os.environ.get("CACHE_URL") or env.get("CACHE_URL")
    if not url:
        redis_unavailable("CACHE_URL not configured")
    cache = RedisCacheProvider(
        name="batch", driver="redis", options={"url": url}, shared=False
    )
    prefix = f"batch-test:{uuid.uuid4().hex}"
    keys = [f"{prefix}:{i}" for i in range(3)]
    try:
        try:
            await cache.set_many({keys[0]: "x", keys[2]: "z"}, ttl_seconds=30)
        except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
            redis_unavailable("Redis unreachable")
        assert await cache.get_many(keys) == ["x", None, "z"]
        client = await cache.redis_client()
        assert 0 < await client.ttl(keys[0]) <= 30
        assert {"get_many", "set_many"} <= set(cache.performance_hints)
    finally:
        client = await cache.redis_client()
        await client.delete(*keys)
        await cache.aclose()
