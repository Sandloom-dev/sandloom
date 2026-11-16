"""Batch cache ops (get_many / set_many). The Redis driver's batch ops are tested
in ``packages/sandloom-redis/tests/test_redis_cache_batch_ops.py``, the Celery
defaults of the ``celery`` taskqueue driver in
``packages/sandloom-celery/tests/test_celery_defaults.py``."""

from __future__ import annotations

import pytest

from sandloom.core.testing import InMemoryCache


@pytest.mark.asyncio
async def test_default_batch_ops_fall_back_to_single_key_ops() -> None:
    cache = InMemoryCache()
    await cache.set_many({"a": "1", "b": "2"}, ttl_seconds=60)
    assert await cache.get_many(["a", "missing", "b"]) == ["1", None, "2"]
    assert await cache.get_many([]) == []
