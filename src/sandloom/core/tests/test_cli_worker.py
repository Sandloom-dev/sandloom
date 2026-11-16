"""``sandloom worker`` / ``sandloom beat`` / ``sandloom worker check``
(``sandloom.core.cli.worker``): each command is a thin layer with one call into
the worker code."""

from __future__ import annotations

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
    assert kernel.worker == {
        "queues": None,
        "concurrency": None,
        "loglevel": "INFO",
    }

    assert (
        worker_cli.worker_main(
            [
                "--queues",
                "mail,default",
                "--concurrency",
                "3",
                "--loglevel",
                "DEBUG",
            ]
        )
        == 0
    )
    assert kernel.worker == {
        "queues": ["mail", "default"],
        "concurrency": 3,
        "loglevel": "DEBUG",
    }


def test_the_worker_has_no_pool_flag_the_pool_is_a_driver_option(
    kernel: _Kernel, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        worker_cli.worker_main(["--pool", "solo"])
    assert exit_info.value.code == 2
    assert "--pool" in capsys.readouterr().err
    assert kernel.worker is None


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


def test_check_exits_with_the_hosts_verdict_and_prints_its_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI turns the host's answer into the exit code (the heartbeat file
    of the Celery host: the Celery package's ``test_celery_cli_worker.py``)."""
    asked: list[float] = []
    verdict = (False, "worker heartbeat /x missing or stale")

    def check(timeout: float) -> tuple[bool, str]:
        asked.append(timeout)
        return verdict

    monkeypatch.setattr(worker_cli, "_check", check)
    assert worker_cli.worker_main(["check"]) == 1
    assert "missing or stale" in capsys.readouterr().err
    verdict = (True, "")
    assert worker_cli.worker_main(["check", "--timeout", "10"]) == 0
    assert capsys.readouterr().err == ""
    assert asked == [worker_cli.DEFAULT_CHECK_TIMEOUT_SECONDS, 10.0]


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


def test_the_tool_infra_configures_the_taskqueue_and_no_other_capability() -> None:
    """The probe asks the worker host: validating the database or the event bus
    would import their drivers (and the web framework) for nothing. The host a
    driver builds: the Celery package's ``test_celery_cli_worker.py``."""
    from sandloom.core.infra.tool_infra import build_tool_infra

    infra = build_tool_infra()
    configured = set(infra._config)
    assert "taskqueue" in configured
    assert configured.isdisjoint({"database", "eventbus", "cache", "objectstore"})


# What the Celery host does with ``sandloom worker`` (the heartbeat check, a worker
# that dies, the Celery CLI it runs and its environment): the Celery package's
# ``test_celery_cli_worker.py``.
