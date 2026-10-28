"""Batch cache ops (get_many / set_many) and the framework Celery defaults."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from celery import Celery

from sandloom.core import worker
from sandloom.core.drivers import celery_taskqueue
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.testing import InMemoryCache
from sandloom.core.testing.workers import worker_env_values


@pytest.mark.asyncio
async def test_default_batch_ops_fall_back_to_single_key_ops() -> None:
    cache = InMemoryCache()
    await cache.set_many({"a": "1", "b": "2"}, ttl_seconds=60)
    assert await cache.get_many(["a", "missing", "b"]) == ["1", None, "2"]
    assert await cache.get_many([]) == []


@pytest.mark.asyncio
async def test_redis_batch_ops_round_trip() -> None:
    env = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    url = os.environ.get("CACHE_URL") or env.get("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not configured")
    cache = RedisCacheProvider(
        name="batch", driver="redis", options={"url": url}, shared=False
    )
    prefix = f"batch-test:{uuid.uuid4().hex}"
    keys = [f"{prefix}:{i}" for i in range(3)]
    try:
        try:
            await cache.set_many({keys[0]: "x", keys[2]: "z"}, ttl_seconds=30)
        except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
            pytest.skip("Redis unreachable")
        assert await cache.get_many(keys) == ["x", None, "z"]
        client = await cache.raw_client()
        assert 0 < await client.ttl(keys[0]) <= 30
        assert {"get_many", "set_many"} <= cache.features()
    finally:
        client = await cache.raw_client()
        await client.delete(*keys)
        await cache.aclose()


def test_worker_reliability_defaults() -> None:
    app = Celery("t", broker="memory://", backend="cache+memory://")
    worker.configure_worker_reliability(app)
    assert app.conf.task_ignore_result is True
    assert app.conf.worker_prefetch_multiplier == 1
    # acks_late stays an explicit per-task opt-in (idempotent tasks only).
    assert not app.conf.task_acks_late


def test_driver_built_apps_ignore_results_by_default() -> None:
    from sandloom.core.infra.config import InstanceConfig

    provider = celery_taskqueue.create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="named",
            driver="celery",
            options={"broker": "memory://", "backend": "cache+memory://"},
        )
    )
    assert provider._app.conf.task_ignore_result is True


@pytest.mark.asyncio
async def test_enqueue_never_subscribes_a_result_consumer() -> None:
    sent: list[dict[str, Any]] = []

    class _App:
        def send_task(self, name: str, **options: Any) -> Any:
            sent.append({"name": name, **options})
            return type("R", (), {"id": "task-1"})()

    provider = celery_taskqueue.CeleryTaskQueueProvider(
        name="x", driver="celery", options={}, app=_App()
    )
    assert await provider.enqueue("mod.task", {"a": 1}) == "task-1"
    assert sent == [
        {
            "name": "mod.task",
            "kwargs": {"a": 1},
            "countdown": None,
            "task_id": None,
            "ignore_result": True,
            "retry": False,
        }
    ]
