"""A Celery broker on the database of ``cache.default`` warns at startup.

The task queue is a persistent store: an evicting cache on the same Redis
database can drop a queued task. ``CeleryTaskQueueProvider.aopen`` registers the
broker's endpoint in ``sandloom.driverkit.shared_endpoints`` (this package
imports no other driver package: the cache's driver registers its own side) and
the rendezvous logs one WARNING naming both instances, never the broker's
password. Nothing is refused. A queue the kernel synthesized is logged at INFO.

Lane: ``celery-drivers`` (no Redis connection is made: ``aopen`` only compares
endpoints).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from sandloom_celery.taskqueue import (
    CeleryTaskQueueProvider,
    create_celery_taskqueue,
)

from sandloom.core.infra.config import InstanceConfig
from sandloom.driverkit.shared_endpoints import register, reset, unregister

_LOGGER = "sandloom.driverkit.shared_endpoints"
_SECRET = "s3cret-pw"


def _queue(broker: str, *, implicit: bool = False) -> CeleryTaskQueueProvider:
    queue = create_celery_taskqueue(
        InstanceConfig("taskqueue", "jobs", "celery", {"broker": broker})
    )
    queue.implicit = implicit  # what the registry sets after building
    return queue


@pytest.fixture(autouse=True)
def _default_cache() -> Iterator[None]:
    reset()
    register("cache", "default", "cache-host:6379/1", "cache_default")
    yield
    reset()


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == level
    ]


async def test_a_broker_on_the_cache_database_warns_without_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = _queue(f"redis://:{_SECRET}@cache-host:6379/1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await queue.aopen()
        await queue.aopen()  # opened again: still one line
    [message] = _records(caplog, logging.WARNING)
    assert "taskqueue instance 'jobs'" in message
    assert "cache.default" in message
    assert "cache-host:6379/1" in message
    assert "taskqueue.jobs.broker" in message
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
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await queue.aopen()
    assert _records(caplog, logging.WARNING) == []
    assert _records(caplog, logging.INFO) == []


async def test_a_queue_nobody_declared_is_info_not_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = _queue("redis://cache-host:6379/1", implicit=True)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await queue.aopen()
    assert _records(caplog, logging.WARNING) == []
    assert len(_records(caplog, logging.INFO)) == 1


async def test_a_closed_queue_is_forgotten(caplog: pytest.LogCaptureFixture) -> None:
    unregister("cache", "default", "cache_default")  # the queue opens first here
    queue = _queue("redis://cache-host:6379/1")
    await queue.aopen()
    await queue.aclose()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        # the cache of a later app registers: the closed queue is not met
        register("cache", "default", "cache-host:6379/1", "cache_default")
    assert _records(caplog, logging.WARNING) == []
