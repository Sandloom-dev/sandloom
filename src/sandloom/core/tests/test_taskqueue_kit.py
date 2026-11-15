"""``TaskQueueConformance`` (spec 8.2): the celery driver passes its send groups
on an in-memory broker and its host groups on a real one, and the kit catches
broken drivers and broken hosts."""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.celery_taskqueue import create_celery_taskqueue
from sandloom.core.drivers.redis_lease import RedisLeaseProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.infra.ports.lease import LeaseProvider
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.driverkit import HealthResult
from sandloom.driverkit.testing import TaskQueueConformance


def _celery(broker: str) -> TaskQueueProvider:
    return create_celery_taskqueue(
        InstanceConfig("taskqueue", "kit", "celery", {"broker": broker})
    )


@contextmanager
def _frozen_beat_clock() -> Iterator[Callable[[], None]]:
    """Celery's clock frozen three seconds before a minute boundary, in every
    process forked while it is entered; ``advance`` moves it past the boundary.

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


class TestCeleryTaskQueue(TaskQueueConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], TaskQueueProvider]:
        return lambda: _celery("memory://")

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

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], TaskQueueProvider]:
        # Nothing listens on port 1; the password is the kit's secret.
        return lambda: _celery(f"redis://:{self.leak_marker}@127.0.0.1:1/0")

    # A group that does not apply here; a ``None`` attribute is not collected (a
    # skip would be a test that runs on no CI lane: tools/ci/skip_ledger.py).
    # Spec 8.2: the unknown-task group is optional for a driver that cannot
    # reject at send: a producer that never booted the task table (an API
    # process) publishes any task name to the broker, and only the worker that
    # consumes it knows the table. The kernel rejects an undeclared task before
    # it reserves a key (kernel-layer tests: test_enqueue_deduplication).
    test_send_of_an_unknown_task_raises_invalid_argument = None  # type: ignore[assignment]


class _Broken(TaskQueueProvider):
    """A driver with one planted defect, selected by ``defect``."""

    contract_version = 2
    supported_features = frozenset({"delay"})

    def __init__(self, defect: str) -> None:
        super().__init__(name="broken", driver="broken", options={})
        self.defect = defect

    def worker_host(self) -> Any:
        raise NotImplementedError

    async def send(
        self,
        task: str,
        payload: bytes,
        *,
        task_id: str,
        delay: timedelta | None = None,
        at_least_once: bool = False,
        queue: str = "default",
    ) -> str:
        if self.defect == "releases-on-failure":
            # The backend is down, yet the error says nothing was published.
            raise InvalidArgument("down", capability="taskqueue")
        if self.defect == "other-id":
            return uuid4().hex
        if self.defect == "accepts-anything":
            return task_id
        if self.defect == "unavailable-after-refusal":
            raise Unavailable("lost", capability="taskqueue")
        return task_id

    async def isolation_identity(self) -> str | None:
        return "broken:kit"


async def test_a_driver_that_releases_the_key_for_a_failing_backend_is_caught() -> None:
    kit = TaskQueueConformance()
    with pytest.raises(AssertionError, match="releases the idempotency key"):
        await kit.test_send_errors_carry_the_dedupe_decision(
            lambda: _Broken("releases-on-failure")
        )
    await kit.test_send_errors_carry_the_dedupe_decision(
        lambda: _Broken("unavailable-after-refusal")
    )


async def test_a_driver_that_answers_another_id_is_caught() -> None:
    kit = TaskQueueConformance()
    with pytest.raises(AssertionError, match="kernel chose the id"):
        await kit.test_send_returns_the_task_id_it_was_given(_Broken("other-id"))


async def test_a_driver_that_accepts_a_payload_that_is_not_json_is_caught() -> None:
    kit = TaskQueueConformance()
    with pytest.raises(pytest.fail.Exception):
        await kit.test_send_refuses_a_payload_that_is_not_a_json_object(
            _Broken("accepts-anything"), b"not json"
        )


# --------------------------------------- host groups against broken hosts
#
# A fake driver whose "broker" is a pipe the forked processes share, and a host
# with one planted defect. Each host group must fail exactly for the defect that
# names it, and the defect-free host passes them all.


class _World:
    """What a fake driver's sender and its hosts share across forks."""

    def __init__(self) -> None:
        context = multiprocessing.get_context("fork")
        self.reader, self.writer = context.Pipe(duplex=False)
        self.lock = context.Lock()
        self.alive = context.Value("i", 0)

    def post(self, message: tuple[str, bytes, str, float]) -> None:
        with self.lock:
            self.writer.send(message)


class _FakeHost:
    def __init__(self, defect: str, world: _World) -> None:
        self.defect = defect
        self.world = world

    def run_worker(self, boot: Any, **options: Any) -> int:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        table = boot.boot()
        boot.before_fork()
        if self.defect != "never-starts":
            boot.on_process_start()
        self.world.alive.value = 1
        pending: list[tuple[float, str, bytes, str]] = []
        while not stop.is_set():
            if self.world.reader.poll(0.05):
                task, payload, task_id, delay = self.world.reader.recv()
                if task in table.tasks or self.defect == "runs-unknown":
                    wait = 0.0 if self.defect == "ignores-delay" else delay
                    pending.append((time.time() + wait, task, payload, task_id))
            for item in [p for p in pending if p[0] <= time.time()]:
                pending.remove(item)
                for _ in range(2 if self.defect == "runs-twice" else 1):
                    boot.run(item[1], item[2], task_id=item[3])
        if self.defect != "check-stays-ok":
            self.world.alive.value = 0
        if self.defect != "never-stops":
            boot.on_process_stop()
        return 0

    def run_scheduler(self, boot: Any, *, lease: Any, lease_name: str, **_: Any) -> int:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        table = boot.boot()
        granted = boot.run_coroutine(
            lease.acquire(lease_name, ttl_seconds=30, owner=str(os.getpid()))
        )
        if granted is not None or self.defect == "ignores-lease":
            for spec in table.periodic:
                self.world.post((spec.task, b"{}", uuid4().hex, 0.0))
        stop.wait()
        return 0

    def check(self, *, timeout_seconds: float) -> HealthResult:
        if self.world.alive.value or self.defect == "check-always-ok":
            return HealthResult(status="ok", details={})
        return HealthResult(status="fail", details={})


class _Hosted(TaskQueueProvider):
    contract_version = 2
    supported_features = frozenset({"delay", "periodic"})

    def __init__(self, defect: str, world: _World) -> None:
        super().__init__(name="hosted", driver="hosted", options={})
        self.defect = defect
        self.world = world

    def worker_host(self) -> Any:
        return _FakeHost(self.defect, self.world)

    async def send(
        self,
        task: str,
        payload: bytes,
        *,
        task_id: str,
        delay: timedelta | None = None,
        at_least_once: bool = False,
        queue: str = "default",
    ) -> str:
        wait = delay.total_seconds() if delay else 0.0
        self.world.post((task, payload, task_id, wait))
        return task_id

    async def isolation_identity(self) -> str | None:
        return "hosted:kit"


def _hosted(defect: str) -> Callable[[], TaskQueueProvider]:
    world = _World()
    return lambda: _Hosted(defect, world)


def _shared_lease() -> LeaseProvider:
    return RedisLeaseProvider(
        name="beat", driver="redis", options={"url": settings.cache_url}
    )


_HOST_GROUPS = [
    ("test_host_runs_a_sync_task_once", "runs-twice", "ran one sent message 2 times"),
    ("test_host_runs_an_async_task_once", "runs-twice", "ran an async task 2 times"),
    (
        "test_host_does_not_run_a_delayed_task_before_its_delay",
        "ignores-delay",
        "3s delay",
    ),
    (
        "test_host_runs_nothing_for_an_unknown_task",
        "runs-unknown",
        "not in its task table",
    ),
    (
        "test_host_keeps_one_loop_per_process_and_stops_each_process",
        "never-starts",
        "never started",
    ),
    (
        "test_host_keeps_one_loop_per_process_and_stops_each_process",
        "never-stops",
        "never stopped",
    ),
    (
        "test_check_reports_a_live_worker",
        "check-always-ok",
        "before any was started",
    ),
    ("test_check_reports_a_live_worker", "check-stays-ok", "after it was stopped"),
]


@pytest.mark.parametrize("group", sorted({g for g, _, _ in _HOST_GROUPS}))
async def test_a_host_without_a_defect_passes_the_host_groups(group: str) -> None:
    await getattr(TaskQueueConformance(), group)(_hosted("none"))


@pytest.mark.parametrize(("group", "defect", "message"), _HOST_GROUPS)
async def test_a_host_with_a_defect_fails_the_group_that_names_it(
    group: str, defect: str, message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        await getattr(TaskQueueConformance(), group)(_hosted(defect))


async def test_a_second_scheduler_that_ignores_the_lease_is_caught() -> None:
    group = TaskQueueConformance().test_scheduler_sends_once_per_slot_while_holding_the_lease
    await group(_hosted("none"), _shared_lease, None)
    with pytest.raises(AssertionError, match="sent the periodic task 2 times"):
        await group(_hosted("ignores-lease"), _shared_lease, None)
