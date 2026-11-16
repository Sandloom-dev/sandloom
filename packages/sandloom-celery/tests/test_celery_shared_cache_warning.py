"""A Celery broker on the database of ``cache.default`` warns at startup.

The task queue is a persistent store: an evicting cache on the same Redis
database can drop a queued task. ``CeleryTaskQueueProvider.aopen`` logs one
WARNING through ``sandloom_redis.shared_cache`` (naming both instances, never
the broker's password) and refuses nothing; without ``sandloom-redis`` there is
no Redis default cache to compare with and nothing is logged.

Lane: ``celery-drivers`` (no Redis connection is made: ``aopen`` only compares
urls).
"""

from __future__ import annotations

import importlib.util
import logging

import pytest
import sandloom_redis.shared_cache as shared_cache
from sandloom_celery.taskqueue import (
    CeleryTaskQueueProvider,
    create_celery_taskqueue,
)

from sandloom.core.infra.config import InstanceConfig

_SECRET = "s3cret-pw"


def _queue(broker: str) -> CeleryTaskQueueProvider:
    return create_celery_taskqueue(
        InstanceConfig("taskqueue", "jobs", "celery", {"broker": broker})
    )


@pytest.fixture(autouse=True)
def _default_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_cache, "_default_cache", "cache-host:6379/1")
    monkeypatch.setattr(shared_cache, "_persistent", {})


async def test_a_broker_on_the_cache_database_warns_without_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = _queue(f"redis://:{_SECRET}@cache-host:6379/1")
    with caplog.at_level(logging.WARNING, logger="sandloom_redis.shared_cache"):
        await queue.aopen()
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = record.getMessage()
    assert "taskqueue instance 'jobs'" in message
    assert "cache.default" in message
    assert "cache-host:6379/1" in message
    assert _SECRET not in caplog.text


@pytest.mark.parametrize(
    "broker",
    [
        "redis://cache-host:6379/2",  # another database
        "redis://broker-host:6379/1",  # another host
        "amqp://guest:guest@cache-host:5672//",  # not a Redis broker
        "memory://",
    ],
)
async def test_another_broker_does_not_warn(
    caplog: pytest.LogCaptureFixture, broker: str
) -> None:
    queue = _queue(broker)
    with caplog.at_level(logging.WARNING, logger="sandloom_redis.shared_cache"):
        await queue.aopen()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_without_sandloom_redis_nothing_is_compared(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "sandloom_redis" else real(name, *a),
    )
    queue = _queue("redis://cache-host:6379/1")
    with caplog.at_level(logging.WARNING, logger="sandloom_redis.shared_cache"):
        await queue.aopen()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
