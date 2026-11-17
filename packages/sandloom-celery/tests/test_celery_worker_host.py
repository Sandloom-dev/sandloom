"""The Celery host of the worker port (spec 8.2, ``sandloom_celery.worker``).

The kernel hands a host a :class:`WorkerBoot`; the Celery host registers the
task table, wires Celery's signals to the boot's process hooks, runs the
heartbeat and the scheduler under the lease, and answers ``check``. Nothing of
Celery is in the kernel: ``test_worker.py`` pins that side and
``sandloom.core.tests.test_worker_port`` the port's own contract.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery, signals
from celery.app.utils import find_app
from sandloom_celery import worker as celery_worker
from sandloom_celery.connection_policy import ConnectionOptions
from sandloom_celery.taskqueue import (
    CeleryTaskQueueProvider,
    create_celery_taskqueue,
)
from sandloom_celery.worker import (
    CeleryWorkerHost,
    WorkerPool,
    configure_worker_reliability,
    configure_worker_sizing,
    heartbeat_is_fresh,
    install_heartbeat,
    register_task_table,
)

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.infra.ports.taskqueue import PeriodicSpec, TaskSpec, TaskTable
from sandloom.core.infra.registry import DriverNotRegisteredError
from sandloom.driverkit import process_config


def _celery() -> Celery:
    return Celery("sandloom-worker-test", broker="memory://", backend="cache+memory://")


@pytest.fixture(autouse=True)
def _billiard_forks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefork pool is refused where billiard spawns (macOS): the tests of
    the host's wiring run as on Linux; the package's ``test_celery_worker_pool`` pins the
    refusal."""
    import billiard  # type: ignore[import-untyped]

    monkeypatch.setattr(billiard, "get_start_method", lambda *a, **k: "fork")


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
def test_the_celery_provider_has_a_worker_host_and_no_escape_hatch() -> None:
    """The port's half (``worker_host`` is abstract, no ``raw_backend``) is
    ``sandloom.core.tests.test_worker_port``."""
    assert "worker_host" in CeleryTaskQueueProvider.__dict__
    assert not hasattr(CeleryTaskQueueProvider, "raw_backend")
    assert not hasattr(CeleryTaskQueueProvider, "native")
    assert not hasattr(CeleryTaskQueueProvider, "register_task_table")


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
    monkeypatch: pytest.MonkeyPatch, pool: WorkerPool
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
    CeleryWorkerHost(lambda: app, worker_pool=pool).run_worker(boot, concurrency=4)
    expected = ["worker", "--loglevel=INFO", f"--pool={pool}"]
    if pool == "threads":
        expected.append("--concurrency=4")
    assert seen["argv"] == expected  # solo runs one message: no concurrency
    # no process is forked: no before_fork (the GC freeze children share)
    assert boot.events == ["boot", "start", "stop"]
    assert len(signals.worker_shutdown.receivers) == before  # disconnected after


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
def _importable_app_infra(
    monkeypatch: pytest.MonkeyPatch, default: dict[str, object] | None
) -> None:
    """The ``INFRA`` the importable app reads through ``Settings`` (no flat
    ``BROKER_URL`` / ``CACHE_URL`` exists: they are removed settings)."""
    monkeypatch.setattr(celery_worker._default, "app", None)
    monkeypatch.setattr(celery_worker._default, "connection", None)
    monkeypatch.setattr(
        settings,
        "infra",
        {} if default is None else {"taskqueue": {"default": default}},
    )
    # the variables Celery's CLI writes and ``app.conf`` reads: none ambient
    for key in (*celery_worker._BROKER_ENV, celery_worker._BACKEND_ENV):
        monkeypatch.delenv(key, raising=False)


def test_the_importable_app_names_the_infra_option_when_it_has_no_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stray BROKER_URL in the environment is no broker of this app.
    monkeypatch.setenv("BROKER_URL", "redis://127.0.0.1:1/7")
    for default in ({"driver": "celery"}, {"driver": "celery", "broker": "  "}):
        _importable_app_infra(monkeypatch, default)
        with pytest.raises(AttributeError, match=r"taskqueue\.default\.broker"):
            celery_worker.app  # noqa: B018
    assert celery_worker._default.app is None


def test_the_importable_app_refuses_a_default_taskqueue_that_is_not_celery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _importable_app_infra(monkeypatch, None)
    monkeypatch.setattr(process_config, "process_instance", lambda *_: None)
    with pytest.raises(AttributeError, match="celery driver.*none"):
        celery_worker.app  # noqa: B018
    # another installed task queue driver on the default instance
    other = InstanceConfig(
        capability="taskqueue", name="default", driver="rq", options={"broker": "x"}
    )
    monkeypatch.setattr(process_config, "process_instance", lambda *_: other)
    with pytest.raises(AttributeError, match="'rq'"):
        celery_worker.app  # noqa: B018
    # a driver nobody installed is the registry's refusal, not an empty answer
    monkeypatch.undo()
    _importable_app_infra(monkeypatch, {"driver": "memory"})
    with pytest.raises(DriverNotRegisteredError, match="taskqueue/default"):
        celery_worker.app  # noqa: B018


def test_the_importable_app_has_no_result_backend_unless_infra_states_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _importable_app_infra(
        monkeypatch, {"driver": "celery", "broker": "redis://127.0.0.1:1/9"}
    )
    assert celery_worker.app.conf.result_backend is None

    _importable_app_infra(
        monkeypatch,
        {
            "driver": "celery",
            "broker": "redis://127.0.0.1:1/9",
            "backend": "redis://127.0.0.1:1/8",
        },
    )
    assert celery_worker.app.conf.result_backend == "redis://127.0.0.1:1/8"


_INFRA_BROKER = "redis://user:infra-secret@127.0.0.1:1/9"


@pytest.mark.parametrize(
    "variable",
    ["CELERY_BROKER_URL", "CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL"],
)
def test_the_importable_app_refuses_a_celery_broker_variable_other_than_infra(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """``celery -b`` (``BROKER_URL``) writes ``CELERY_BROKER_URL`` and ``app.conf``
    prefers it over the app's broker: ``inspect`` / ``call`` / Flower would use
    a broker INFRA does not state. The refusal names the variable, never a URL."""
    _importable_app_infra(monkeypatch, {"driver": "celery", "broker": _INFRA_BROKER})
    monkeypatch.setenv(variable, "redis://user:stray-secret@127.0.0.1:1/7")
    with pytest.raises(AttributeError) as refused:
        celery_worker.app  # noqa: B018
    assert variable in str(refused.value)
    assert "stray-secret" not in str(refused.value)
    assert "infra-secret" not in str(refused.value)
    assert celery_worker._default.app is None


def test_the_importable_app_accepts_celery_variables_equal_to_infra_or_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI mirrors ``-b`` into ``CELERY_BROKER_URL``: the value INFRA states
    passes, and a blank one is unset to Celery."""
    _importable_app_infra(
        monkeypatch,
        {
            "driver": "celery",
            "broker": _INFRA_BROKER,
            "backend": "redis://127.0.0.1:1/8",
        },
    )
    monkeypatch.setenv("CELERY_BROKER_URL", _INFRA_BROKER)
    monkeypatch.setenv("CELERY_BROKER_READ_URL", "")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:1/8")
    assert celery_worker.app.conf.broker_url == _INFRA_BROKER


def test_the_importable_app_refuses_a_result_backend_variable_other_than_infra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for backend in (None, "redis://127.0.0.1:1/8"):
        options: dict[str, object] = {"driver": "celery", "broker": _INFRA_BROKER}
        if backend is not None:
            options["backend"] = backend
        _importable_app_infra(monkeypatch, options)
        monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://u:stray-secret@h:1/3")
        with pytest.raises(AttributeError, match="CELERY_RESULT_BACKEND") as refused:
            celery_worker.app  # noqa: B018
        assert "stray-secret" not in str(refused.value)
        assert celery_worker._default.app is None


def _config_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, body: str
) -> None:
    """A Celery config module on ``sys.path``, named by ``CELERY_CONFIG_MODULE``
    (what ``celery --config`` writes)."""
    (tmp_path / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setenv("CELERY_CONFIG_MODULE", name)


@pytest.mark.parametrize(
    ("setting", "backend"),
    [
        # ``broker_url`` of a config module loses to the app's own ``broker=``, as
        # does ``result_backend`` where INFRA states one: only what INFRA leaves
        # out (or the read / write url, which it never states) can differ.
        ("broker_write_url", None),
        ("broker_write_url", "redis://127.0.0.1:1/8"),
        ("broker_read_url", None),
        ("broker_read_url", "redis://127.0.0.1:1/8"),
        ("result_backend", None),
    ],
)
def test_the_importable_app_refuses_a_config_module_that_changes_a_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    setting: str,
    backend: str | None,
) -> None:
    """``celery --config X``, ``CELERY_CONFIG_MODULE`` and ``--loader default``
    set ``broker_write_url`` / ``broker_read_url`` / ``result_backend`` after the
    environment check: the finished conf is compared with INFRA, and the refusal
    names the option, never a URL."""
    options: dict[str, object] = {"driver": "celery", "broker": _INFRA_BROKER}
    if backend is not None:
        options["backend"] = backend
    _importable_app_infra(monkeypatch, options)
    _config_module(
        monkeypatch,
        tmp_path,
        "foreign_celery_config_a",
        f'{setting} = "redis://user:foreign-secret@127.0.0.1:1/5"\n',
    )
    with pytest.raises(AttributeError, match=setting) as refused:
        celery_worker.app  # noqa: B018
    assert "foreign-secret" not in str(refused.value)
    assert "infra-secret" not in str(refused.value)
    # the second lookup finds the cached app and refuses it just the same
    with pytest.raises(AttributeError, match=setting):
        celery_worker.app  # noqa: B018


@pytest.mark.parametrize(
    "setting",
    [
        'broker_transport = "memory"',
        "broker_port = 6399",
        'broker_vhost = "7"',
        'broker_user = "stray-user"',
        'broker_password = "foreign-secret"',
        'broker_host = "stray-host"',
        # a ``broker_use_ssl`` dict is merged into the connection after the url
        "broker_use_ssl = {'host': 'stray-host', 'port': 6399, "
        "'password': 'foreign-secret', 'virtual_host': '7'}",
        "import ssl; broker_use_ssl = {'ssl_cert_reqs': ssl.CERT_NONE}",
        # True makes a rediss:// connection fall back to plain text
        "broker_use_ssl = True",
        # transport options are laid over the connection by some transports
        "broker_transport_options = {'host': '127.0.0.1:6399', "
        "'userid': 'stray-user', 'password': 'foreign-secret'}",
        "result_backend_transport_options = {'host': 'stray-host', "
        "'password': 'foreign-secret'}",
        # the redis result backend takes these where its url omits the part
        "redis_port = 6399",
        'redis_password = "foreign-secret"',
        'redis_username = "stray-user"',
        "redis_backend_use_ssl = {'ssl_cert_reqs': 'none'}",
    ],
)
def test_the_importable_app_refuses_a_config_module_that_changes_the_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setting: str
) -> None:
    """The url of the conf is INFRA's, but kombu also takes the transport, host,
    port, vhost and credentials from the conf and lets them win: a config module
    setting only one redirects (``memory`` sinks) the worker and every
    ``inspect`` / ``call`` of the importable app. Refused, the key named."""
    key = setting.removeprefix("import ssl; ").split(" ")[0]
    _importable_app_infra(
        monkeypatch, {"driver": "celery", "broker": "redis://127.0.0.1"}
    )
    _config_module(monkeypatch, tmp_path, "foreign_celery_config_c", setting + "\n")
    with pytest.raises(AttributeError, match=key) as refused:
        celery_worker.app  # noqa: B018
    assert "foreign-secret" not in str(refused.value)
    assert celery_worker._default.app is None


@pytest.mark.parametrize(
    ("broker", "setting"),
    [
        # kombu's pyamqp transport lays the options over host, userid, password
        (
            "amqp://u:infra-secret@127.0.0.1:1//",
            "broker_transport_options = {'host': '127.0.0.1:6399', "
            "'userid': 'stray-user', 'password': 'foreign-secret'}",
        ),
        # ... and ``ssl`` turns certificate verification off for amqps://
        (
            "amqps://u:infra-secret@127.0.0.1:1//",
            "import ssl; broker_transport_options = "
            "{'ssl': {'cert_reqs': ssl.CERT_NONE}}",
        ),
    ],
)
def test_the_importable_app_refuses_a_transport_option_that_moves_an_amqp_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, broker: str, setting: str
) -> None:
    """The importable app is not limited to redis: an amqp broker stays supported,
    its transport options outside the allow-list are refused, the option named."""
    _importable_app_infra(monkeypatch, {"driver": "celery", "broker": broker})
    _config_module(monkeypatch, tmp_path, "foreign_celery_config_d", setting + "\n")
    with pytest.raises(AttributeError, match="broker_transport_options") as refused:
        celery_worker.app  # noqa: B018
    text = str(refused.value)
    for stray in ("foreign-secret", "infra-secret", "stray", "6399", "CERT_NONE"):
        assert stray not in text
    assert celery_worker._default.app is None


def test_the_importable_app_accepts_a_harmless_transport_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _importable_app_infra(
        monkeypatch, {"driver": "celery", "broker": "amqp://u:p@127.0.0.1:1//"}
    )
    _config_module(
        monkeypatch,
        tmp_path,
        "foreign_celery_config_d",
        "broker_transport_options = {'confirm_publish': True}\n",
    )
    assert celery_worker.app.conf.broker_transport_options == {"confirm_publish": True}


def _celery_instances_in_the_module() -> list[str]:
    """What ``celery.app.utils.find_app`` scans for once ``app`` / ``celery`` raise."""
    return [
        name for name, value in vars(celery_worker).items() if isinstance(value, Celery)
    ]


@pytest.mark.parametrize("provider_first", [False, True])
def test_celery_dash_a_finds_no_app_to_use_after_a_foreign_conf_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_first: bool
) -> None:
    """``celery -A sandloom_celery.worker`` resolves through ``find_app``, which
    scans ``vars(module)`` for a ``Celery`` instance after ``app`` and ``celery``
    raise: the refused app must not be there (it would run with the foreign conf
    and no ``_sandloom_importable`` mark), whether the lookup built it or the
    provider did."""
    _importable_app_infra(monkeypatch, {"driver": "celery", "broker": _INFRA_BROKER})
    if provider_first:
        create_celery_taskqueue(
            InstanceConfig(
                capability="taskqueue",
                name="default",
                driver="celery",
                options={"broker": _INFRA_BROKER},
            )
        )
    _config_module(
        monkeypatch,
        tmp_path,
        "foreign_celery_config_c",
        'broker_write_url = "redis://user:foreign-secret@127.0.0.1:1/5"\n',
    )
    with pytest.raises(AttributeError, match="broker_write_url") as refused:
        find_app("sandloom_celery.worker")
    assert "foreign-secret" not in str(refused.value)
    assert _celery_instances_in_the_module() == []
    if not provider_first:
        # nothing of the refused app is kept for the next lookup or the provider
        assert celery_worker._default.app is None
        assert celery_worker._default.connection is None


def test_celery_dash_a_finds_the_marked_importable_app_when_the_conf_is_infras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the scan test: with nothing foreign, ``find_app`` returns
    the process-wide app, marked importable (so ``worker`` / ``beat`` refuse)."""
    _importable_app_infra(monkeypatch, {"driver": "celery", "broker": _INFRA_BROKER})
    monkeypatch.delenv("CELERY_CONFIG_MODULE", raising=False)
    found = find_app("sandloom_celery.worker")
    assert found is celery_worker._default.app
    assert found._sandloom_importable is True
    assert _celery_instances_in_the_module() == []


def test_the_importable_app_refuses_the_default_loaders_celeryconfig(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--loader default`` / ``CELERY_LOADER=default`` imports ``celeryconfig.py``
    from the working directory."""
    _importable_app_infra(monkeypatch, {"driver": "celery", "broker": _INFRA_BROKER})
    (tmp_path / "celeryconfig.py").write_text(
        'broker_write_url = "redis://user:foreign-secret@127.0.0.1:1/5"\n'
    )
    monkeypatch.delitem(sys.modules, "celeryconfig", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CELERY_LOADER", "default")
    with pytest.raises(AttributeError, match="broker_write_url") as refused:
        celery_worker.app  # noqa: B018
    assert "foreign-secret" not in str(refused.value)


def test_the_importable_app_refuses_a_foreign_conf_where_the_provider_built_it_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cached ``_default`` app skips the build, not the check. Built here
    through ``get_default_app`` (the provider checks the final conf of the app it
    builds, which loads the config module: one set later would never apply)."""
    _importable_app_infra(monkeypatch, {"driver": "celery", "broker": _INFRA_BROKER})
    built = celery_worker.get_default_app(ConnectionOptions(_INFRA_BROKER))
    assert built is celery_worker._default.app is not None
    _config_module(
        monkeypatch,
        tmp_path,
        "foreign_celery_config_b",
        'broker_write_url = "redis://user:foreign-secret@127.0.0.1:1/5"\n'
        'result_backend = "redis://user:foreign-secret@127.0.0.1:1/6"\n',
    )
    with pytest.raises(AttributeError, match="broker_write_url") as refused:
        celery_worker.app  # noqa: B018
    assert "foreign-secret" not in str(refused.value)


def test_the_importable_app_accepts_a_config_module_that_states_what_infra_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = "redis://127.0.0.1:1/8"
    _importable_app_infra(
        monkeypatch,
        {"driver": "celery", "broker": _INFRA_BROKER, "backend": backend},
    )
    _config_module(
        monkeypatch,
        tmp_path,
        "same_celery_config",
        f'broker_write_url = "{_INFRA_BROKER}"\nresult_backend = "{backend}"\n'
        "worker_prefetch_multiplier = 3\n",
    )
    app = celery_worker.app
    assert app.conf.broker_write_url == _INFRA_BROKER
    assert app.conf.worker_prefetch_multiplier == 3


def test_the_importable_app_is_the_default_app_built_from_the_infra_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The environment is not read: this stray value must not replace the INFRA one.
    monkeypatch.setenv("BROKER_URL", "redis://127.0.0.1:1/7")
    _importable_app_infra(
        monkeypatch, {"driver": "celery", "broker": "redis://127.0.0.1:1/9"}
    )
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
    """``celery -A sandloom_celery.worker worker`` would consume
    messages of a table with no task (each dropped as unknown) and a beat would
    tick without the lease: both stop with exit code 1 and name the command."""
    _importable_app_infra(
        monkeypatch, {"driver": "celery", "broker": "redis://127.0.0.1:1/9"}
    )
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
