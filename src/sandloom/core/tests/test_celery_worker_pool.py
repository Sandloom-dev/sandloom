"""The ``worker_pool`` option of the ``celery`` driver (spec 8.2).

The process model is the driver's own option, not an argument of the port: the
host refuses a pool the process cannot run before the kernel boots anything, a
``solo`` / ``threads`` worker keeps its heartbeat from a thread (a message runs
inside the timer's event loop), and no pool that forks nothing is told to freeze
the heap for children.
"""

from __future__ import annotations

import time
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery
from pydantic import ValidationError
from sandloom_celery import worker as celery_worker
from sandloom_celery.taskqueue import (
    CeleryTaskQueueOptions,
    create_celery_taskqueue,
)
from sandloom_celery.worker import (
    CeleryWorkerHost,
    WorkerPool,
    heartbeat_is_fresh,
    install_heartbeat,
    refuse_unusable_pool,
)

from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.infra.ports.taskqueue import TaskTable


class _Boot:
    def __init__(self) -> None:
        self.events: list[str] = []

    def boot(self) -> TaskTable:
        self.events.append("boot")
        return TaskTable(tasks={}, periodic=[])

    def run(self, task: str, payload: bytes, *, task_id: str) -> None:
        self.events.append("run")

    def before_fork(self) -> None:
        self.events.append("before_fork")

    def on_process_start(self) -> None:
        self.events.append("start")

    def on_process_stop(self) -> None:
        self.events.append("stop")

    def run_coroutine(self, coro: Coroutine[Any, Any, Any]) -> Any:
        raise AssertionError("a worker runs no coroutine here")


def _start_method(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    import billiard  # type: ignore[import-untyped]

    monkeypatch.setattr(billiard, "get_start_method", lambda *a, **k: method)


def _host(pool: WorkerPool) -> CeleryWorkerHost:
    app = Celery("pool-test", broker="memory://", set_as_current=False)
    return CeleryWorkerHost(lambda: app, worker_pool=pool)


# ------------------------------------------------------------------ the option
def test_the_pool_is_a_typed_option_defaulting_to_prefork() -> None:
    assert CeleryTaskQueueOptions().worker_pool == "prefork"
    for pool in ("prefork", "solo", "threads"):
        assert CeleryTaskQueueOptions(worker_pool=pool).worker_pool == pool
    with pytest.raises(ValidationError, match="worker_pool"):
        CeleryTaskQueueOptions.model_validate({"worker_pool": "gevent"})


@pytest.mark.parametrize("pool", ["prefork", "solo", "threads"])
def test_the_provider_hands_its_host_the_pool_of_its_options(pool: str) -> None:
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="hosted",
            driver="celery",
            options={"broker": "redis://127.0.0.1:1/0", "worker_pool": pool},
        )
    )
    host = provider.worker_host()
    assert isinstance(host, CeleryWorkerHost)
    assert host._worker_pool == pool


def test_a_bad_pool_is_refused_when_the_instance_is_built() -> None:
    with pytest.raises(ValueError, match="worker_pool"):
        create_celery_taskqueue(
            InstanceConfig(
                capability="taskqueue",
                name="hosted",
                driver="celery",
                options={"broker": "redis://127.0.0.1:1/0", "worker_pool": "gevent"},
            )
        )


# ------------------------------------------- prefork where billiard cannot fork
@pytest.mark.parametrize("method", ["spawn", "forkserver"])
def test_prefork_is_refused_before_boot_where_billiard_does_not_fork(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    _start_method(monkeypatch, method)
    seen: list[Any] = []
    monkeypatch.setattr(
        celery_worker, "_run_celery", lambda app, argv: seen.append(argv)
    )
    boot = _Boot()
    with pytest.raises(InvalidArgument, match="worker_pool.*solo.*threads"):
        _host("prefork").run_worker(boot)
    assert boot.events == []  # nothing booted
    assert seen == []  # nothing ran


@pytest.mark.parametrize("pool", ["solo", "threads"])
def test_a_pool_without_children_runs_where_billiard_spawns(
    monkeypatch: pytest.MonkeyPatch, pool: WorkerPool
) -> None:
    _start_method(monkeypatch, "spawn")
    monkeypatch.setattr(celery_worker, "_run_celery", lambda app, argv: 0)
    boot = _Boot()
    assert _host(pool).run_worker(boot) == 0
    assert boot.events == ["boot"]  # the host starts no process outside Celery's run


def test_prefork_runs_where_billiard_forks(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_method(monkeypatch, "fork")
    refuse_unusable_pool("prefork", {})


# -------------------------------------------- settings a pool without children ignores
@pytest.mark.parametrize("pool", ["solo", "threads"])
@pytest.mark.parametrize(
    "key", ["WORKER_MAX_MEMORY_PER_CHILD", "WORKER_MAX_TASKS_PER_CHILD"]
)
def test_a_recycle_setting_with_a_pool_that_has_no_children_is_refused(
    pool: WorkerPool, key: str
) -> None:
    with pytest.raises(InvalidArgument, match=key):
        refuse_unusable_pool(pool, {key: "100"})
    refuse_unusable_pool(pool, {})  # unset: fine


def test_a_recycle_setting_is_fine_with_prefork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start_method(monkeypatch, "fork")
    refuse_unusable_pool(
        "prefork",
        {"WORKER_MAX_MEMORY_PER_CHILD": "100", "WORKER_MAX_TASKS_PER_CHILD": "5"},
    )


def test_the_host_refuses_a_recycle_setting_before_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_MAX_TASKS_PER_CHILD", "10")
    boot = _Boot()
    with pytest.raises(InvalidArgument, match="WORKER_MAX_TASKS_PER_CHILD"):
        _host("solo").run_worker(boot)
    assert boot.events == []


def test_solo_with_an_explicit_concurrency_above_one_is_refused() -> None:
    with pytest.raises(InvalidArgument, match="WORKER_CONCURRENCY"):
        refuse_unusable_pool("solo", {"WORKER_CONCURRENCY": "4"})
    refuse_unusable_pool("solo", {"WORKER_CONCURRENCY": "1"})
    refuse_unusable_pool("solo", {})
    refuse_unusable_pool("threads", {"WORKER_CONCURRENCY": "4"})


# ------------------------------------------------------ the heartbeat of solo
def _heartbeat_step(app: Celery, parent: Any) -> Any:
    (step_cls,) = [s for s in app.steps["worker"] if "Heartbeat" in s.__name__]
    return step_cls(parent)


def test_a_blocking_task_keeps_the_solo_heartbeat_fresh(tmp_path: Path) -> None:
    """``solo`` runs a message inside the event loop that fires the timer: a task
    longer than the check window starves it. The thread keeps the file fresh
    while the loop (this test's main thread) is blocked."""
    beat = tmp_path / "hb"
    app = Celery("hb-thread", broker="memory://", set_as_current=False)
    install_heartbeat(app, str(beat), thread=True, interval=0.05)

    class _Timer:
        def call_repeatedly(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the thread replaces the worker timer")

    parent = SimpleNamespace(timer=_Timer())
    step = _heartbeat_step(app, parent)
    step.start(parent)
    try:
        time.sleep(0.6)  # a task blocking the loop for twelve intervals
        assert heartbeat_is_fresh(str(beat), max_age=0.3)
    finally:
        step.stop(parent)
    assert not beat.exists()  # a stopped worker is never alive
    assert step.ticker is not None and not step.ticker.is_alive()
    time.sleep(0.15)
    assert not beat.exists()  # the stopped thread never resurrects it


def test_the_prefork_heartbeat_stays_on_the_worker_timer(tmp_path: Path) -> None:
    """Without ``thread`` a blocked timer leaves the file stale: the thread is
    what keeps the solo one fresh."""
    beat = tmp_path / "hb"
    app = Celery("hb-timer", broker="memory://", set_as_current=False)
    install_heartbeat(app, str(beat), interval=0.05)
    armed: list[float] = []

    class _Timer:
        def call_repeatedly(self, secs: float, *args: Any, **kwargs: Any) -> Any:
            armed.append(secs)
            return SimpleNamespace(cancel=lambda: None)

    parent = SimpleNamespace(timer=_Timer())
    step = _heartbeat_step(app, parent)
    step.start(parent)
    try:
        time.sleep(0.3)  # the (fake) timer never fires: as a starved loop
        assert armed == [0.05]
        assert step.ticker is None
        assert not heartbeat_is_fresh(str(beat), max_age=0.2)
    finally:
        step.stop(parent)


def test_the_host_installs_the_thread_heartbeat_only_off_prefork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start_method(monkeypatch, "fork")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        celery_worker,
        "install_heartbeat",
        lambda app, path=None, **kw: seen.setdefault("kw", []).append(kw),
    )
    monkeypatch.setattr(celery_worker, "_run_celery", lambda app, argv: 0)
    for pool in ("prefork", "solo", "threads"):
        _host(pool).run_worker(_Boot())
    assert seen["kw"] == [{"thread": False}, {"thread": True}, {"thread": True}]


# ------------------------------------------------- the clock of a beat's slots
def test_the_slot_of_a_send_is_read_on_the_clock_the_beat_ticks_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick due on the app's clock claims the slot of THAT moment: with the
    real clock beside it, a tick due in one minute could claim the next one's
    slot (a slot claim too short to dedupe, or one the lease's next holder
    does not see)."""
    from datetime import UTC, datetime

    from celery import signals
    from celery.schedules import crontab

    from sandloom.driverkit.periodic import SlotClaim

    frozen = datetime(2026, 3, 4, 10, 7, 3, tzinfo=UTC)
    app = Celery("slot-clock", broker="memory://", set_as_current=False)
    monkeypatch.setattr(Celery, "now", lambda self: frozen)
    claimed: list[str] = []

    class _Lease:
        name = "lease"

        def claim_slot(
            self, periodic_id: str, schedule: Any, zone: Any, slot: Any
        ) -> Any:
            claimed.append(slot.key)
            return SlotClaim("claimed")

    receiver = celery_worker.install_beat_lease(app, _Lease())  # type: ignore[arg-type]
    try:
        inner = SimpleNamespace(
            app=app, apply_async=lambda entry, producer=None, advance=True, **kw: "sent"
        )

        class _Service:  # a signal's sender must be hashable
            scheduler = inner

        service = _Service()
        service.app = app  # type: ignore[attr-defined]
        signals.beat_init.send(sender=service)
        entry = SimpleNamespace(name="every-minute", schedule=crontab())
        assert inner.apply_async(entry) == "sent"
    finally:
        celery_worker.remove_beat_lease(receiver)
    assert claimed == ["20260304T100700Z"]  # the app's moment, not today's
