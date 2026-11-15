"""``sandloom worker`` / ``sandloom beat`` / ``sandloom worker check``
(``sandloom.core.cli.worker``): each command is a thin layer with one call into
the worker code."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import worker as worker_cli

#: The settings a worker process loads (nothing is connected to: the heartbeat
#: check reads a file and the dying worker uses an in-memory broker).
_SERVICE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/db",
    "CACHE_URL": "redis://127.0.0.1:1/0",
    "BROKER_URL": "redis://127.0.0.1:1/1",
    "JWT_SECRET": "x" * 40,
}


class _Kernel:
    """Stands in for ``sandloom.core.worker``: records what the CLI asks of it."""

    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.worker: dict[str, Any] | None = None
        self.beat: dict[str, Any] | None = None

    def run_worker(self, **kwargs: Any) -> int:
        self.worker = kwargs
        return self.result

    def run_scheduler(self, **kwargs: Any) -> int:
        self.beat = kwargs
        return self.result


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> _Kernel:
    fake = _Kernel()
    monkeypatch.setattr(worker_cli, "_worker", fake)
    return fake


def test_worker_runs_the_worker_with_its_options(kernel: _Kernel) -> None:
    assert worker_cli.worker_main([]) == 0
    assert kernel.worker == {"queues": None, "concurrency": None, "loglevel": "INFO"}

    assert (
        worker_cli.worker_main(
            ["--queues", "mail,default", "--concurrency", "3", "--loglevel", "DEBUG"]
        )
        == 0
    )
    assert kernel.worker == {
        "queues": ["mail", "default"],
        "concurrency": 3,
        "loglevel": "DEBUG",
    }


def test_worker_returns_the_exit_code_of_the_process(kernel: _Kernel) -> None:
    kernel.result = 7
    assert worker_cli.worker_main([]) == 7


def test_beat_runs_the_scheduler(kernel: _Kernel) -> None:
    assert worker_cli.beat_main(["--schedule", "/tmp/celerybeat-schedule"]) == 0
    assert kernel.beat == {
        "loglevel": "INFO",
        "state_file": "/tmp/celerybeat-schedule",
    }
    assert worker_cli.beat_main([]) == 0
    assert kernel.beat == {"loglevel": "INFO", "state_file": None}


@pytest.mark.parametrize("value", ["0", "-2", "many"])
def test_worker_concurrency_must_be_a_positive_integer(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        worker_cli.worker_main(["--concurrency", value])
    assert exc.value.code == 2
    assert "positive integer" in capsys.readouterr().err


def test_check_follows_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    beat = tmp_path / "heartbeat"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(beat))

    assert worker_cli.worker_main(["check"]) == 1
    assert "missing or stale" in capsys.readouterr().err

    beat.touch()
    assert worker_cli.worker_main(["check"]) == 0

    old = time.time() - 30
    os.utime(beat, (old, old))
    assert worker_cli.worker_main(["check"]) == 0
    assert worker_cli.worker_main(["check", "--timeout", "10"]) == 1


@pytest.mark.parametrize(
    "value", ["0", "-1", "inf", "-inf", "nan", "86401", "1e9", "x"]
)
def test_check_timeout_must_be_a_finite_positive_number(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """``inf`` / ``nan`` would make a dead worker's heartbeat count as fresh forever."""
    with pytest.raises(SystemExit) as exc:
        worker_cli.worker_main(["check", f"--timeout={value}"])
    assert exc.value.code == 2
    assert "positive number" in capsys.readouterr().err


def test_check_timeout_accepts_the_ceiling() -> None:
    assert worker_cli._positive_seconds("86400") == 86400.0
    assert worker_cli._positive_seconds("0.5") == 0.5


def _sandloom(*argv: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    code = f"import sys; sys.argv = ['sandloom', *{list(argv)!r}]; import sandloom; sandloom.main()"
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.subprocess_probe
def test_worker_check_is_registered_and_boots_nothing(tmp_path: Path) -> None:
    """Through the ``sandloom`` entry point; imports no Celery, boots no root."""
    beat = tmp_path / "heartbeat"
    env: dict[str, Any] = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")
        if name in os.environ
    }
    env.update(_SERVICE_ENV)
    env["WORKER_HEARTBEAT_FILE"] = str(beat)

    stale = _sandloom("worker", "check", env=env)
    assert stale.returncode == 1
    assert "missing or stale" in stale.stderr

    beat.touch()
    assert _sandloom("worker", "check", env=env).returncode == 0

    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['sandloom', 'worker', 'check']; import sandloom\n"
            "try:\n    sandloom.main()\nexcept SystemExit as e:\n"
            "    print(e.code, 'celery' in sys.modules, 'fastapi' in sys.modules,\n"
            "          'sandloom.core.app_factory.lifecycle' in sys.modules)\n",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # No Celery, no web framework, no application lifecycle: the container probe
    # runs every 30 s with a 5 s timeout.
    assert loaded.stdout.split() == ["0", "False", "False", "False"], loaded.stderr


_DYING_WORKER = """
import sys
from celery import Celery, bootsteps

import sandloom.core.worker as worker
from sandloom.core.cli import worker as worker_cli
from sandloom.core.drivers.celery_worker import CeleryWorkerHost

app = Celery("dies", broker="memory://", set_as_current=False)
app.conf.worker_pool = "solo"


class Dies(bootsteps.StartStopStep):
    def start(self, parent):
        raise RuntimeError("the worker dies on start")


app.steps["worker"].add(Dies)


class Provider:
    driver = "celery"
    options = {}

    def worker_host(self):
        return CeleryWorkerHost(lambda: app)

    def features(self):
        return frozenset({"delay", "periodic", "redelivery"})


class Core:
    loaded_modules = []

    def taskqueue(self, name="default"):
        return Provider()


worker.load_composition_core = lambda: Core()
sys.exit(worker_cli.worker_main(["--concurrency", "1", "--loglevel", "CRITICAL"]))
"""


@pytest.mark.subprocess_probe
def test_a_worker_that_dies_exits_non_zero() -> None:
    """Celery's ``app.start()`` drops the process exit code: ``sandloom worker``
    must not report 0 for a worker that died (the container would not restart).

    A real Celery app (memory broker, solo pool) in its own process: a worker
    changes process-wide state (logging, signals, the current app, GC), which
    must not reach the other tests of this one.
    """
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")
        if name in os.environ
    }
    env.update(_SERVICE_ENV)
    done = subprocess.run(
        [sys.executable, "-c", _DYING_WORKER],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "the worker dies on start" in done.stderr + done.stdout
    assert done.returncode != 0, done.stderr


def test_celery_commands_bind_the_app_and_return_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host runs Celery's CLI bound to its app, and the CLI's return value
    is the process exit code (``Celery.start()`` drops it)."""
    from celery import Celery
    from celery.bin.celery import celery as celery_cli

    from sandloom.core.drivers import celery_worker

    app = Celery("bound", broker="memory://", set_as_current=False)
    seen: dict[str, Any] = {}

    def fake_main(*, args: list[str], standalone_mode: bool) -> int:
        seen.update(args=args, standalone=standalone_mode)
        seen["bound"] = celery_cli.params[0].default
        return 3

    monkeypatch.setattr(celery_cli, "main", fake_main)
    assert celery_worker._run_celery(app, ["beat", "--loglevel=INFO"]) == 3
    # the host's own broker goes first, explicitly; then the command
    assert seen["args"] == ["--broker", "memory://", "beat", "--loglevel=INFO"]
    assert seen["standalone"] is False
    assert seen["bound"] is app
    assert celery_cli.params[0].default is None


def test_the_tool_infra_configures_the_taskqueue_and_no_other_capability() -> None:
    """The probe asks the worker host: validating the database or the event bus
    would import their drivers (and the web framework) for nothing."""
    from sandloom.core.infra.tool_infra import build_tool_infra

    infra = build_tool_infra()
    configured = set(infra._config)
    assert "taskqueue" in configured
    assert configured.isdisjoint({"database", "eventbus", "cache", "objectstore"})
    host = infra.provider("taskqueue", "default").worker_host()
    assert hasattr(host, "check")


_ENV_ISOLATION_NAMES = (
    "APP",
    "BROKER_URL",
    "RESULT_BACKEND",
    "LOADER",
    "CONFIG_MODULE",
    "CELERY_APP",
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
    "CELERY_LOADER",
    "CELERY_CONFIG_MODULE",
)


def test_celery_cli_env_cannot_replace_the_hosts_broker_or_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BROKER_URL`` fills the CLI's ``--broker`` and ``CELERY_BROKER_URL`` wins
    over ``app.conf`` on every read: an instance with its own broker must keep
    it (its lease scope was computed from it), and ``APP`` must not swap the app
    (the beat lease is installed on the host's)."""
    from celery import Celery

    from sandloom.core.drivers import celery_worker

    app = Celery("mine", broker="memory://mine", set_as_current=False)
    for name in _ENV_ISOLATION_NAMES:
        monkeypatch.setenv(name, "memory://hijacked" if "URL" in name else "other.app")
    seen: dict[str, Any] = {}

    from celery.bin.celery import celery as celery_cli

    def fake_main(*, args: list[str], standalone_mode: bool) -> int:
        seen["bound"] = celery_cli.params[0].default
        seen["envvars"] = {
            p.name: p.envvar for p in celery_cli.params if p.name == "broker"
        }
        seen["conf_broker"] = app.conf.broker_url
        seen["environ"] = {n: os.environ.get(n) for n in _ENV_ISOLATION_NAMES}
        seen["args"] = args
        return 0

    monkeypatch.setattr(celery_cli, "main", fake_main)
    assert celery_worker._run_celery(app, ["worker"]) == 0
    assert seen["conf_broker"] == "memory://mine"
    assert seen["envvars"] == {"broker": None}  # the CLI reads no env for it
    assert seen["bound"] is app
    assert seen["args"][:2] == ["--broker", "memory://mine"]
    for name in ("CELERY_BROKER_URL", "CELERY_APP", "CELERY_BROKER_READ_URL"):
        assert seen["environ"][name] is None, name
    # restored for the rest of the process, option envvars included
    assert os.environ["CELERY_BROKER_URL"] == "memory://hijacked"
    assert os.environ["BROKER_URL"] == "memory://hijacked"
    assert os.environ["APP"] == "other.app"
    assert [p.envvar for p in celery_cli.params if p.name == "broker"] == ["BROKER_URL"]


def test_celery_cli_runs_the_hosts_app_whatever_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through the real CLI: ``celery report`` prints the app it ran on."""
    from celery import Celery

    from sandloom.core.drivers import celery_worker

    app = Celery("mine", broker="memory://mine", set_as_current=False)
    for name in _ENV_ISOLATION_NAMES:
        monkeypatch.setenv(name, "memory://hijacked" if "URL" in name else "other.app")
    assert celery_worker._run_celery(app, ["report"]) == 0
    out = capsys.readouterr().out
    assert "memory://mine" in out
    assert "hijacked" not in out
