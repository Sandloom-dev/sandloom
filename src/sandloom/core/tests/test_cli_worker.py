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


class _FakeApp:
    def __init__(self, result: object = None) -> None:
        self.argv: list[str] | None = None
        self.result = result

    def start(self, argv: list[str]) -> object:
        self.argv = argv
        return self.result


def _patch_app(monkeypatch: pytest.MonkeyPatch, app: _FakeApp) -> None:
    import sandloom.core.worker as worker

    monkeypatch.setattr(worker, "get_worker_app", lambda: app)


def test_worker_runs_the_worker_with_its_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _FakeApp()
    _patch_app(monkeypatch, app)

    assert worker_cli.worker_main([]) == 0
    assert app.argv == ["worker", "--loglevel=INFO"]

    assert (
        worker_cli.worker_main(
            ["--queues", "mail,default", "--concurrency", "3", "--loglevel", "DEBUG"]
        )
        == 0
    )
    assert app.argv == [
        "worker",
        "--loglevel=DEBUG",
        "--queues=mail,default",
        "--concurrency=3",
    ]


def test_worker_returns_the_exit_code_of_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_app(monkeypatch, _FakeApp(result=7))
    assert worker_cli.worker_main([]) == 7


def test_beat_runs_the_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _FakeApp()
    _patch_app(monkeypatch, app)

    assert worker_cli.beat_main(["--schedule", "/tmp/celerybeat-schedule"]) == 0
    assert app.argv == [
        "beat",
        "--loglevel=INFO",
        "--schedule=/tmp/celerybeat-schedule",
    ]


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


def test_check_timeout_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        worker_cli.worker_main(["check", "--timeout", "0"])
    assert exc.value.code == 2
    assert "positive number" in capsys.readouterr().err


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
            "    print(e.code, 'celery' in sys.modules)\n",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert loaded.stdout.split() == ["0", "False"], loaded.stderr
