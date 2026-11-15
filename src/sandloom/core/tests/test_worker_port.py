"""The worker port (spec 8.2) and its Celery host (``sandloom.core.drivers.celery_worker``).

The kernel hands a host a :class:`WorkerBoot`; the Celery host registers the
task table, wires Celery's signals to the boot's process hooks, runs the
heartbeat and the scheduler under the lease, and answers ``check``. Nothing of
Celery is in the kernel: ``test_worker.py`` pins that side.
"""

from __future__ import annotations

import os
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery, signals

from sandloom.core.drivers import celery_worker
from sandloom.core.drivers.celery_taskqueue import (
    CeleryTaskQueueProvider,
    create_celery_taskqueue,
)
from sandloom.core.drivers.celery_worker import (
    CeleryWorkerHost,
    configure_worker_reliability,
    configure_worker_sizing,
    heartbeat_is_fresh,
    install_heartbeat,
    register_task_table,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.infra.ports.taskqueue import (
    PeriodicSpec,
    TaskQueueProvider,
    TaskSpec,
    TaskTable,
)


def _celery() -> Celery:
    return Celery("sandloom-worker-test", broker="memory://", backend="cache+memory://")


class _Boot:
    """A ``WorkerBoot`` that records every call the host makes."""

    def __init__(self, table: TaskTable | None = None) -> None:
        self.table = table or TaskTable(tasks={}, periodic=[])
        self.events: list[str] = []
        self.ran: list[tuple[str, bytes, str]] = []

    def boot(self) -> TaskTable:
        self.events.append("boot")
        return self.table

    def run(self, task: str, payload: bytes, *, task_id: str) -> None:
        self.ran.append((task, payload, task_id))

    def before_fork(self) -> None:
        self.events.append("before_fork")

    def on_process_start(self) -> None:
        self.events.append("start")

    def on_process_stop(self) -> None:
        self.events.append("stop")

    def run_coroutine(self, coro: Coroutine[Any, Any, Any]) -> Any:
        import asyncio

        return asyncio.run(coro)


# ----------------------------------------------------------------- the port
def test_the_provider_door_has_a_worker_host_and_no_escape_hatch() -> None:
    assert "worker_host" in TaskQueueProvider.__abstractmethods__
    assert not hasattr(TaskQueueProvider, "raw_backend")
    assert not hasattr(CeleryTaskQueueProvider, "raw_backend")
    assert not hasattr(CeleryTaskQueueProvider, "native")
    assert not hasattr(CeleryTaskQueueProvider, "register_task_table")


def test_a_driver_without_a_host_cannot_be_built() -> None:
    class _NoHost(TaskQueueProvider):
        async def send(self, *args: Any, **kwargs: Any) -> str:
            return "x"

    with pytest.raises(TypeError, match="worker_host"):
        _NoHost(name="n", driver="d", options={})  # type: ignore[abstract]


def test_the_celery_provider_answers_its_host_without_connecting() -> None:
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="hosted",
            driver="celery",
            options={"broker": "redis://127.0.0.1:1/0"},
        )
    )
    assert provider._built_app is None  # built on first use, not at creation
    host = provider.worker_host()
    assert isinstance(host, CeleryWorkerHost)
    assert provider._built_app is None  # a check needs no Celery app
    assert host._app is provider._app


# -------------------------------------------------------- sizing and defaults
def test_sizing_applies_the_kernel_concurrency_and_the_recycle_knobs() -> None:
    app = _celery()
    configure_worker_sizing(app, concurrency=3, env={})
    assert app.conf.worker_concurrency == 3
    assert app.conf.worker_max_memory_per_child is None

    configure_worker_sizing(
        app,
        concurrency=None,
        env={
            "WORKER_MAX_MEMORY_PER_CHILD": "200000",
            "WORKER_MAX_TASKS_PER_CHILD": "500",
        },
    )
    assert app.conf.worker_concurrency == 3  # not overwritten by None
    assert app.conf.worker_max_memory_per_child == 200000
    assert app.conf.worker_max_tasks_per_child == 500


@pytest.mark.parametrize("raw", ["0", "-1", "two"])
def test_sizing_rejects_bad_values(raw: str) -> None:
    with pytest.raises(InvalidArgument, match="WORKER_MAX_TASKS_PER_CHILD"):
        configure_worker_sizing(
            _celery(), concurrency=1, env={"WORKER_MAX_TASKS_PER_CHILD": raw}
        )


def test_reliability_defaults_prefetch_one_and_ignore_results() -> None:
    app = _celery()
    configure_worker_reliability(app)
    assert app.conf.task_ignore_result is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert not app.conf.task_acks_late


# ---------------------------------------------------------------- heartbeat
def test_heartbeat_freshness(tmp_path: Path) -> None:
    beat = tmp_path / "hb"
    assert not heartbeat_is_fresh(str(beat), max_age=60)
    beat.touch()
    assert heartbeat_is_fresh(str(beat), max_age=60)
    assert not heartbeat_is_fresh(str(beat), max_age=60, now=beat.stat().st_mtime + 120)


def test_heartbeat_step_runs_on_the_worker_timer(tmp_path: Path) -> None:
    """The bootstep touches the file on start, re-arms on the worker's timer
    and removes the file on stop (a stopped worker is never "alive")."""
    beat = tmp_path / "hb"
    app = _celery()
    install_heartbeat(app, str(beat))
    install_heartbeat(app, str(beat))  # one step per app
    (step_cls,) = [s for s in app.steps["worker"] if "Heartbeat" in s.__name__]
    assert "celery.worker.components:Timer" in step_cls.requires

    scheduled: list[tuple[float, Any]] = []

    class _Timer:
        def call_repeatedly(
            self, secs: float, fun: Any, args: Any, priority: int
        ) -> Any:
            scheduled.append((secs, fun))
            return SimpleNamespace(cancel=lambda: scheduled.append((-1, None)))

    parent = SimpleNamespace(timer=_Timer())
    step = step_cls(parent)
    step.start(parent)
    assert heartbeat_is_fresh(str(beat), max_age=60)
    assert scheduled[0][0] == celery_worker.HEARTBEAT_INTERVAL_SECONDS
    beat.unlink()
    scheduled[0][1]()  # a timer tick re-touches the file
    assert beat.exists()
    step.stop(parent)
    assert not beat.exists()
    assert scheduled[-1] == (-1, None)  # timer entry cancelled
    scheduled[0][1]()  # a late tick after stop never resurrects it
    assert not beat.exists()


def test_check_follows_the_heartbeat_and_names_no_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    beat = tmp_path / "heartbeat"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(beat))
    app = Celery("s7", broker="redis://user:s3cret@broker.invalid:6379/0")
    host = CeleryWorkerHost(lambda: app)

    down = host.check(timeout_seconds=60)
    assert down.status == "fail"
    assert "missing or stale" in str(down.details)

    beat.touch()
    up = host.check(timeout_seconds=60)
    assert up.status == "ok"

    old = beat.stat().st_mtime - 30
    os.utime(beat, (old, old))
    assert host.check(timeout_seconds=60).status == "ok"
    stale = host.check(timeout_seconds=10)
    assert stale.status == "fail"
    for result in (down, up, stale):
        text = str(result.details)
        assert "s3cret" not in text
        assert "broker.invalid" not in text
        assert "redis://" not in text


# -------------------------------------------------------------- task table
def _table() -> TaskTable:
    return TaskTable(
        tasks={
            "m.tasks.job": TaskSpec(),
            "m.tasks.mail": TaskSpec(queue="mail"),
            "m.tasks.safe": TaskSpec(at_least_once=True),
        },
        periodic=[PeriodicSpec(id="m.job", cron="*/5 * * * *", task="m.tasks.job")],
    )


def test_register_task_table_registers_tasks_routes_and_the_schedule() -> None:
    app = _celery()
    boot = _Boot()
    register_task_table(app, _table(), boot.run)
    app.finalize()

    assert {"m.tasks.job", "m.tasks.mail", "m.tasks.safe"} <= set(app.tasks)
    assert app.conf.task_routes == {"m.tasks.mail": {"queue": "mail"}}
    assert app.tasks["m.tasks.safe"].acks_late is True
    assert app.tasks["m.tasks.safe"].reject_on_worker_lost is True
    assert not app.tasks["m.tasks.job"].acks_late
    assert app.conf.beat_schedule["m.job"]["task"] == "m.tasks.job"


def test_a_message_runs_through_the_boot_with_its_id() -> None:
    app = _celery()
    boot = _Boot()
    register_task_table(app, _table(), boot.run)
    app.finalize()

    app.tasks["m.tasks.job"].apply(kwargs={"amount": 7}, task_id="job-1")
    assert boot.ran == [("m.tasks.job", b'{"amount":7}', "job-1")]


# --------------------------------------------------------------- the worker
def _fake_celery(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]) -> None:
    def run(app: Any, argv: Any) -> int:
        seen["argv"] = list(argv)
        seen["receivers"] = {
            name: len(getattr(signals, name).receivers)
            for name in (
                "worker_init",
                "worker_process_init",
                "worker_process_shutdown",
            )
        }
        signals.worker_init.send(sender=None)
        signals.worker_process_init.send(sender=None)
        signals.worker_process_shutdown.send(sender=None)
        return 5

    monkeypatch.setattr(celery_worker, "_run_celery", run)


def test_the_worker_host_boots_registers_and_wires_the_process_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    _fake_celery(monkeypatch, seen)
    before = {
        name: len(getattr(signals, name).receivers)
        for name in ("worker_init", "worker_process_init", "worker_process_shutdown")
    }
    app = _celery()
    boot = _Boot(_table())
    code = CeleryWorkerHost(lambda: app).run_worker(
        boot, queues=["mail", "default"], concurrency=3, loglevel="debug"
    )

    assert code == 5  # the exit code is the process's, not 0
    assert seen["argv"] == [
        "worker",
        "--loglevel=DEBUG",
        "--pool=prefork",
        "--queues=mail,default",
        "--concurrency=3",
    ]
    assert boot.events == ["boot", "before_fork", "start", "stop"]
    assert app.conf.worker_concurrency == 3
    assert app.conf.worker_prefetch_multiplier == 1
    assert "m.tasks.job" in app.tasks
    # connected for the run, disconnected after it
    assert all(seen["receivers"][name] == before[name] + 1 for name in before)
    assert before == {name: len(getattr(signals, name).receivers) for name in before}


def test_the_worker_host_defaults_to_the_default_queue_and_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    _fake_celery(monkeypatch, seen)
    app = _celery()
    CeleryWorkerHost(lambda: app).run_worker(_Boot())
    assert seen["argv"] == ["worker", "--loglevel=INFO", "--pool=prefork"]
    CeleryWorkerHost(lambda: app).run_worker(_Boot(), loglevel="nonsense")
    assert seen["argv"] == ["worker", "--loglevel=INFO", "--pool=prefork"]


@pytest.mark.parametrize("pool", ["solo", "threads"])
def test_a_pool_without_children_starts_and_stops_the_process_in_the_host(
    monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    """solo and threads run the messages in the worker process: Celery sends
    no ``worker_process_init`` / ``worker_process_shutdown`` there, so the host
    starts the process on ``worker_init`` and stops it on ``worker_shutdown``."""
    seen: dict[str, Any] = {}

    def run(app: Any, argv: Any) -> int:
        seen["argv"] = list(argv)
        signals.worker_init.send(sender=None)
        signals.worker_shutdown.send(sender=None)
        return 0

    monkeypatch.setattr(celery_worker, "_run_celery", run)
    boot = _Boot(_table())
    app = _celery()
    before = len(signals.worker_shutdown.receivers)
    CeleryWorkerHost(lambda: app).run_worker(boot, pool=pool)
    assert seen["argv"] == ["worker", "--loglevel=INFO", f"--pool={pool}"]
    assert boot.events == ["boot", "before_fork", "start", "stop"]
    assert len(signals.worker_shutdown.receivers) == before  # disconnected after


def test_the_worker_host_refuses_an_unknown_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    _fake_celery(monkeypatch, seen)
    boot = _Boot(_table())
    app = _celery()
    with pytest.raises(InvalidArgument, match="pool must be one of"):
        CeleryWorkerHost(lambda: app).run_worker(boot, pool="gevent")
    assert "argv" not in seen
    assert boot.events == []  # refused before the kernel boots anything


def test_the_scheduler_argv_carries_the_schedule_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.drivers.memory_lease import MemoryLeaseProvider

    argv: list[Any] = []
    monkeypatch.setattr(
        celery_worker, "_run_celery", lambda app, a: argv.extend(a) or 0
    )
    boot = _Boot(_table())
    store = MemoryLeaseProvider(name="beat", driver="memory", options={})
    app = _celery()
    code = CeleryWorkerHost(lambda: app).run_scheduler(
        boot,
        lease=store,
        lease_name="sandloom:beat:lease:t",
        lease_ttl_seconds=30,
        loglevel="WARNING",
        state_file="/tmp/celerybeat-schedule",
    )
    assert code == 0
    assert argv == [
        "beat",
        "--loglevel=WARNING",
        "--schedule=/tmp/celerybeat-schedule",
    ]
    assert boot.events == ["boot", "stop"]  # no fork: no before_fork, pools disposed


# ----------------------------------------------------- the importable app
def test_the_importable_app_is_the_default_app_built_from_the_broker_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(celery_worker, "_default_app", None)
    monkeypatch.setattr(celery_worker, "_default_app_urls", None)
    monkeypatch.delenv("BROKER_URL", raising=False)
    with pytest.raises(AttributeError, match="BROKER_URL"):
        celery_worker.app  # noqa: B018

    monkeypatch.setenv("BROKER_URL", "redis://127.0.0.1:1/9")
    app = celery_worker.app
    assert app.conf.broker_url == "redis://127.0.0.1:1/9"
    assert celery_worker.celery is app and celery_worker.celery_app is app
    assert app.conf.task_ignore_result is True
    # the provider of the same broker shares it
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="default",
            driver="celery",
            options={"broker": "redis://127.0.0.1:1/9"},
        )
    )
    assert provider._app is app
    with pytest.raises(AttributeError):
        celery_worker.nothing_else  # noqa: B018


class _Sender:
    """What Celery sends a signal as: a ``WorkController`` / ``beat.Service``
    (hashable, with the app it runs)."""

    def __init__(self, app: Any) -> None:
        self.app = app


def test_the_importable_app_refuses_to_run_a_worker_or_a_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``celery -A sandloom.core.drivers.celery_worker worker`` would consume
    messages of a table with no task (each dropped as unknown) and a beat would
    tick without the lease: both stop with exit code 1 and name the command."""
    monkeypatch.setattr(celery_worker, "_default_app", None)
    monkeypatch.setattr(celery_worker, "_default_app_urls", None)
    monkeypatch.setenv("BROKER_URL", "redis://127.0.0.1:1/9")
    app = celery_worker.app

    for signal in (signals.worker_init, signals.beat_init):
        with pytest.raises(SystemExit) as refused:
            signal.send(sender=_Sender(app))
        assert refused.value.code == 1
        # a process of another app, or a signal with no sender, is none of its business
        signal.send(sender=_Sender(_celery()))
        signal.send(sender=None)

    # a host run marks its app: the same default app then runs normally
    app._sandloom_hosted = True
    signals.worker_init.send(sender=_Sender(app))
    signals.beat_init.send(sender=_Sender(app))


def test_the_refusals_point_to_the_sandloom_commands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _celery()
    celery_worker._refuse_unhosted_runs(app)
    for signal, command in (
        (signals.worker_init, "`sandloom worker`"),
        (signals.beat_init, "`sandloom beat`"),
    ):
        caplog.clear()
        with pytest.raises(SystemExit):
            signal.send(sender=_Sender(app))
        assert command in caplog.text
