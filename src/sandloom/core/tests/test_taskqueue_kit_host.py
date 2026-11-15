"""``TaskQueueConformance`` host groups (spec 8.2) against the celery driver: a
worker host and two schedulers, each in a forked process, on the real test
broker and the real test cache. About a minute: its own file, so
``--dist loadfile`` runs it beside the send groups, not after them."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sandloom_celery.taskqueue import create_celery_taskqueue
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.ports.lease import LeaseProvider
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.driverkit.testing import TaskQueueConformance

_HOST_GROUP_PREFIXES = ("test_host_", "test_scheduler_", "test_check_")


def _celery(broker: str) -> TaskQueueProvider:
    return create_celery_taskqueue(
        InstanceConfig("taskqueue", "kit", "celery", {"broker": broker})
    )


@contextmanager
def _frozen_beat_clock() -> Iterator[Callable[[], None]]:
    """Celery's clock frozen three seconds before a minute boundary, in every
    process forked while it is entered; ``advance`` moves it past the boundary
    (and it stays there: one slot, however long the test takes).

    The beat asks the app for the time (``Celery.now``) to tell when a cron
    entry is due, so a scheduler forked under it does not wait for a real minute.
    """
    from celery import Celery

    elapsed = multiprocessing.get_context("fork").Value("d", 0.0)
    base = (time.time() // 60 + 1) * 60 - 3

    def now(self: Any) -> datetime:
        return datetime.fromtimestamp(base + elapsed.value, UTC)

    def advance() -> None:
        elapsed.value = 6.0

    with patch.object(Celery, "now", now):
        yield advance


class TestCeleryTaskQueueHost(TaskQueueConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], TaskQueueProvider]:
        return lambda: _celery(settings.broker_url)

    @pytest.fixture
    def make_host_provider(self) -> Callable[[], TaskQueueProvider]:
        # The forked hosts and the test share the broker: not ``memory://``.
        return lambda: _celery(settings.broker_url)

    @pytest.fixture
    def make_lease_store(self) -> Callable[[], LeaseProvider]:
        return lambda: RedisLeaseProvider(
            name="beat", driver="redis", options={"url": settings.cache_url}
        )

    @pytest.fixture
    def scheduler_clock(
        self,
    ) -> Callable[[], AbstractContextManager[Callable[[], None]]]:
        return _frozen_beat_clock

    @pytest.fixture(autouse=True)
    def _pool_processes_fork(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # billiard spawns pool processes on macOS, and a spawned process never
        # sees the task table the kit's boot registered in its host process.
        # (A no-op where billiard forks already: Linux.)
        from billiard import context  # type: ignore[import-untyped]

        monkeypatch.setattr(
            context._default_context,
            "_actual_context",
            context._default_context.get_context("fork"),
        )

    @pytest.fixture(autouse=True)
    def _heartbeat_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # ``check`` reads the worker's heartbeat file; a test's own file keeps
        # one run's worker from answering for another's.
        monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))


# The send groups are collected in ``test_taskqueue_kit.py``; a ``None`` attribute
# is not collected.
for _group in dir(TaskQueueConformance):
    if _group.startswith("test_") and not _group.startswith(_HOST_GROUP_PREFIXES):
        setattr(TestCeleryTaskQueueHost, _group, None)
