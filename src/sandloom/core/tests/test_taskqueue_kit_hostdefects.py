"""``TaskQueueConformance`` host groups against broken hosts (spec 8.2).

A fake driver whose "broker" is a pipe the forked processes share, and a host
with one planted defect. Each host group must fail exactly for the defect that
names it, and the defect-free host passes them all. Its own file: these fork
processes and run about half a minute (``--dist loadfile``).
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.redis_lease import RedisLeaseProvider
from sandloom.core.infra.ports.lease import LeaseProvider
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.driverkit import HealthResult
from sandloom.driverkit.testing import TaskQueueConformance


class _World:
    """What a fake driver's sender and its hosts share across forks."""

    def __init__(self) -> None:
        context = multiprocessing.get_context("fork")
        self.reader, self.writer = context.Pipe(duplex=False)
        self.lock = context.Lock()
        self.alive = context.Value("i", 0)
        #: A scheduler sent the periodic slot (the fake's per-slot dedupe).
        self.slot_sent = context.Value("i", 0)

    def post(self, message: tuple[str, bytes, str, float]) -> None:
        with self.lock:
            self.writer.send(message)

    def claim_slot(self) -> bool:
        """Mark the one slot sent; ``False`` when it already was."""
        with self.lock:
            first = not self.slot_sent.value
            self.slot_sent.value = 1
            return first


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
        """Send the slot once on holding the lease, until stopped; a standby
        keeps trying the lease and takes it over when the holder is gone."""
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        table = boot.boot()
        held = None
        sent = False
        while not stop.is_set():
            if held is None:
                held = boot.run_coroutine(
                    lease.acquire(lease_name, ttl_seconds=30, owner=str(os.getpid()))
                )
            if (held is not None or self.defect == "ignores-lease") and not sent:
                sent = True
                # A slot the previous holder sent is not sent again, unless the
                # host forgets that a takeover is the same slot.
                if (
                    self.defect in ("ignores-lease", "resends-on-takeover")
                    or self.world.claim_slot()
                ):
                    for spec in table.periodic:
                        self.world.post((spec.task, b"{}", uuid4().hex, 0.0))
            stop.wait(0.2)
        if held is not None:
            boot.run_coroutine(lease.release(held))
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
        if self.defect == "no-host-raises":
            raise NotImplementedError
        if self.defect == "no-host-none":
            return None
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
    # worker_host is abstract: a driver without a host fails, it does not skip.
    ("test_host_runs_a_sync_task_once", "no-host-raises", "answered no host"),
    ("test_host_runs_a_sync_task_once", "no-host-none", "answered no host"),
    ("test_check_reports_a_live_worker", "no-host-none", "answered no host"),
]

#: The groups every defect-free host passes.
_PASSING_GROUPS = sorted({g for g, _, _ in _HOST_GROUPS})


@pytest.mark.parametrize("group", _PASSING_GROUPS)
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


async def test_a_host_that_resends_the_slot_on_takeover_is_caught() -> None:
    group = TaskQueueConformance().test_scheduler_that_takes_over_inside_a_slot_does_not_send_it_again
    await group(_hosted("none"), _shared_lease, None)
    with pytest.raises(AssertionError, match="was sent 2 times"):
        await group(_hosted("resends-on-takeover"), _shared_lease, None)


async def test_a_driver_that_declares_periodic_without_a_lease_store_fails() -> None:
    for name in (
        "test_scheduler_sends_once_per_slot_while_holding_the_lease",
        "test_scheduler_that_takes_over_inside_a_slot_does_not_send_it_again",
    ):
        with pytest.raises(AssertionError, match="make_lease_store"):
            await getattr(TaskQueueConformance(), name)(_hosted("none"), None, None)


async def test_a_driver_without_periodic_skips_the_scheduler_groups() -> None:
    class _NoPeriodic(_Hosted):
        supported_features = frozenset({"delay"})

    world = _World()
    for name in (
        "test_scheduler_sends_once_per_slot_while_holding_the_lease",
        "test_scheduler_that_takes_over_inside_a_slot_does_not_send_it_again",
    ):
        with pytest.raises(pytest.skip.Exception):
            await getattr(TaskQueueConformance(), name)(
                lambda: _NoPeriodic("none", world), None, None
            )
