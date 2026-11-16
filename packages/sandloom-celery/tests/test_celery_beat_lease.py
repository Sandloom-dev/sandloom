"""The Celery side of the beat singleton lease (``sandloom_celery.worker``), over
a ``lease`` store (spec 5; review-map H-10).

Two beats on one broker must not both schedule: the second stands by, takes
over when the first releases its lease or dies (TTL expiry), and a holder
that loses the lease stops at once. The kernel's holder (``FencedLease``) and
its resolution of the lease are ``sandloom.core.tests.test_beat_lease``, which
also holds the stores and the clock these tests borrow.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

import pytest
from celery import Celery, signals
from celery.beat import Service
from sandloom_celery import worker as celery_worker
from sandloom_celery.worker import (
    BeatLease,
    CeleryWorkerHost,
    LeasedScheduler,
    install_beat_lease,
    remove_beat_lease,
)

from sandloom.core.infra.errors import Unavailable
from sandloom.core.infra.fenced_lease import FencedLease
from sandloom.core.tests.test_beat_lease import _NAME, _Clock, _Refusing, _Store


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _pair(ttl: int = 30) -> tuple[_Clock, _Store, BeatLease, BeatLease]:
    clock = _Clock()
    store = _Store(clock)
    first = BeatLease(
        store, _NAME, ttl_seconds=ttl, runner=_run, clock=clock, identity="a:1"
    )
    second = BeatLease(
        store, _NAME, ttl_seconds=ttl, runner=_run, clock=clock, identity="b:2"
    )
    return clock, store, first, second


def test_second_beat_stands_by_and_takes_over_after_release() -> None:
    clock, _store, first, second = _pair()

    assert first.ensure() is True
    assert second.ensure() is False
    assert first.fence == 1 and second.token is None

    first.release()
    clock.now += second.renew_interval
    assert second.ensure() is True
    assert second.fence == 2  # fencing numbers only grow


def test_standby_takes_over_after_the_holder_dies() -> None:
    clock, store, first, second = _pair(ttl=30)
    assert first.ensure() is True
    assert second.ensure() is False

    # The holder crashes: no renewals, the lease's TTL runs out.
    clock.now += 31
    assert second.ensure() is True
    assert second.fence == 2
    assert _holder(store) == ("b:2", 2)


def _holder(store: _Store) -> tuple[str, int]:
    held = store._held[_NAME]
    return held.owner, held.fence


def test_holder_renews_and_keeps_the_lease() -> None:
    clock, store, first, second = _pair(ttl=30)
    assert first.ensure() is True
    for _ in range(10):  # 10 renew periods > 3 TTLs
        clock.now += first.renew_interval
        assert first.ensure() is True
        assert second.ensure() is False
    assert store._held[_NAME].token == first.token


def test_a_holder_that_lost_its_token_stops_scheduling_at_once() -> None:
    from sandloom.core.drivers.memory_lease import _Held

    clock, store, first, _second = _pair(ttl=30)
    assert first.ensure() is True
    # taken over: another grant of the name
    store._held[_NAME] = _Held("other:1", "other-token", 99, clock.now + 30)

    clock.now += first.renew_interval
    assert first.ensure() is False
    assert first.held is False


def test_cache_outage_keeps_scheduling_only_until_the_local_deadline() -> None:
    clock, store, first, _second = _pair(ttl=30)
    assert first.ensure() is True
    store.broken = True

    clock.now += first.renew_interval
    assert first.ensure() is True  # still inside the lease the store granted
    clock.now = 1000.0 + 30  # the local deadline (from before the write)
    assert first.ensure() is False
    assert first.held is False
    # A beat that never held the lease does not schedule through an outage.
    clock.now += 100
    assert first.ensure() is False


def test_leased_scheduler_ticks_only_while_holding() -> None:
    clock, _store, first, second = _pair()

    class _Inner:
        ticks = 0
        closed = False
        max_interval = 300

        def tick(self) -> float:
            self.ticks += 1
            return 120.0

        def close(self) -> None:
            self.closed = True

    held_inner, standby_inner = _Inner(), _Inner()
    held = LeasedScheduler(held_inner, first)
    standby = LeasedScheduler(standby_inner, second)

    assert held.tick() == pytest.approx(first.renew_interval)  # capped to renew
    assert standby.tick() == pytest.approx(second.renew_interval)
    assert held_inner.ticks == 1 and standby_inner.ticks == 0
    assert standby.max_interval == 300  # everything else is the inner's

    held.close()
    assert held_inner.closed and first.held is False
    clock.now += second.renew_interval
    standby.tick()
    assert standby_inner.ticks == 1  # took over after the clean shutdown


def test_beat_init_wraps_the_scheduler_of_this_app_only(tmp_path: Any) -> None:
    clock = _Clock()
    store = _Store(clock)
    app = Celery("lease-test", broker="memory://")
    other = Celery("other", broker="memory://")
    lease = BeatLease(store, _NAME, ttl_seconds=30, runner=_run, clock=clock)
    standby = BeatLease(store, _NAME, ttl_seconds=30, runner=_run, clock=clock)
    receiver = install_beat_lease(app, lease)
    standby_receiver = install_beat_lease(app, standby)
    try:
        service = Service(app, schedule_filename=str(tmp_path / "schedule"))
        signals.beat_init.send(sender=service)
        assert isinstance(service.scheduler, LeasedScheduler)
        assert service.scheduler.lease is lease  # the first receiver wraps once

        # A scheduler on another app (``celery -A other beat``, the ``APP`` env)
        # would run WITHOUT the lease: the process stops, it never schedules.
        foreign = Service(other, schedule_filename=str(tmp_path / "other"))
        with pytest.raises(SystemExit) as refused:
            signals.beat_init.send(sender=foreign)
        assert refused.value.code == 1
        assert not isinstance(foreign.scheduler, LeasedScheduler)
        with pytest.raises(SystemExit) as no_sender:
            signals.beat_init.send(sender=None)
        assert no_sender.value.code == 1

        assert service.scheduler.tick() > 0
        assert lease.held
        assert standby.ensure() is False  # a second beat on the name stands by
        service.scheduler.close()
    finally:
        remove_beat_lease(receiver)
        remove_beat_lease(standby_receiver)


class _Boot:
    """A ``WorkerBoot`` for the Celery host: a table of no task, loop per call."""

    def __init__(self, table: Any | None = None) -> None:
        from sandloom.core.infra.ports.taskqueue import TaskTable

        self.table = table or TaskTable(tasks={}, periodic=[])
        self.stopped = 0

    def boot(self) -> Any:
        return self.table

    def run(self, task: str, payload: bytes, *, task_id: str) -> None: ...

    def before_fork(self) -> None: ...

    def on_process_start(self) -> None: ...

    def on_process_stop(self) -> None:
        self.stopped += 1

    def run_coroutine(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return _run(coro)


def test_the_beat_lease_wraps_the_kernel_holder_without_subclassing_it() -> None:
    _clock, _store, first, _second = _pair()
    assert not issubclass(BeatLease, FencedLease)
    assert isinstance(first._holder, FencedLease)
    assert first._holder.label == "[beat]"


def _fake_beat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, started: list[Any]
) -> None:
    """Replace the Celery command with a ``Service`` whose ``beat_init`` fires."""

    def run(app: Any, argv: Any) -> int:
        service = Service(app, schedule_filename=str(tmp_path / "schedule"))
        signals.beat_init.send(sender=service)
        started.append(service.scheduler)
        return 0

    monkeypatch.setattr(celery_worker, "_run_celery", run)


def test_beat_refuses_to_start_on_a_backend_that_refuses_with_the_real_cause(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started: list[Any] = []
    _fake_beat(monkeypatch, tmp_path, started)
    app = Celery("lease-refused", broker="memory://")
    host = CeleryWorkerHost(lambda: app)
    store = _Refusing(name="beat", driver="memory", options={})
    boot = _Boot()
    with caplog.at_level("CRITICAL", logger="sandloom_celery.worker"):
        code = host.run_scheduler(boot, lease=store, lease_name=_NAME)
    assert code == 1
    assert started == []  # beat never started: no first tick
    assert "allkeys-lru" in caplog.text  # the policy, not "cannot reach"


def test_beat_starts_on_a_backend_that_is_only_unreachable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Down(_Refusing):
        error = Unavailable

    started: list[Any] = []
    _fake_beat(monkeypatch, tmp_path, started)
    app = Celery("lease-down", broker="memory://")
    host = CeleryWorkerHost(lambda: app)
    boot = _Boot()
    store = _Down(name="beat", driver="memory", options={})
    # stands by and retries, no exit
    assert host.run_scheduler(boot, lease=store, lease_name=_NAME) == 0
    (scheduler,) = started
    assert isinstance(scheduler, LeasedScheduler)
    scheduler.close()
    assert boot.stopped == 1  # the process's pools are disposed at the end


async def test_two_leases_on_real_redis(real_cache: str) -> None:
    """Acquire / standby / release / takeover against the real redis lease driver."""
    from sandloom_redis.lease import RedisLeaseProvider

    store = RedisLeaseProvider(name="beat", driver="redis", options={"url": real_cache})
    await store.aopen()
    loop = asyncio.get_running_loop()

    def runner(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)

    name = f"sandloom:beat:lease:redis-test-{os.getpid()}-{id(store)}"
    first = BeatLease(store, name, ttl_seconds=3, runner=runner, identity="a:1")
    second = BeatLease(store, name, ttl_seconds=3, runner=runner, identity="b:2")
    try:
        assert await asyncio.to_thread(first.ensure) is True
        assert await asyncio.to_thread(second.ensure) is False
        holder = await store.holder(name)
        assert holder is not None and holder.owner == "a:1"

        await asyncio.to_thread(first.release)
        assert await store.holder(name) is None
        second._holder._retry_at = 0.0  # skip the standby back-off
        assert await asyncio.to_thread(second.ensure) is True
        assert second.fence is not None and first.fence is not None
        assert second.fence > first.fence

        # The holder "dies": its lease expires on the server after the TTL
        # (polled until gone, not a fixed sleep).
        for _ in range(300):
            if await store.holder(name) is None:
                break
            await asyncio.sleep(0.02)
        assert await store.holder(name) is None
        first._holder._retry_at = 0.0
        assert await asyncio.to_thread(first.ensure) is True
        second._holder._renew_at = 0.0
        assert await asyncio.to_thread(second.ensure) is False  # lost, stops
    finally:
        await asyncio.to_thread(first.release)
        await store.aclose()
