"""``sandloom dev run`` beside its worker and beat: the processes it starts, their
prefixed output, an early exit, and that nothing outlives the run.

The app server is a stand-in (a fake child) and the worker and the beat are real
short Python processes, so what is proved about the stop is proved on real pids:
each test ends with them reaped and with the status the stop gave them."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sandloom.core.cli.dev import commands, process
from sandloom.core.cli.dev.identity import Identity

#: A worker stand-in: reports what it was started with, then runs until stopped.
_WORKER = (
    "import os, time\n"
    "print('probe=' + os.environ.get('DEV_RUN_PROBE', 'missing'), "
    "'cwd=' + os.getcwd(), "
    "'unbuffered=' + os.environ.get('PYTHONUNBUFFERED', ''), flush=True)\n"
    "print('second line', flush=True)\n"
    "time.sleep(300)\n"
)
#: A beat stand-in: writes to stderr (the relay merges it with stdout).
_BEAT = (
    "import sys, time\nprint('tick', file=sys.stderr, flush=True)\ntime.sleep(300)\n"
)
_EARLY_EXIT = "import sys\nprint('boom', flush=True)\nsys.exit(3)\n"


def _stand_ins() -> dict[str, list[str]]:
    return {
        "worker": [sys.executable, "-c", _WORKER],
        "beat": [sys.executable, "-c", _BEAT],
    }


def _eventually(check: Callable[[], object], what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class _Web:
    """The app server child: its first ``wait`` returns once ``until`` holds or
    it was sent a signal (raising ``KeyboardInterrupt`` first when ``interrupt``,
    as Ctrl-C does); a later ``wait`` returns at once, as after a stop."""

    def __init__(self, until: Callable[[], object], *, interrupt: bool = False) -> None:
        self._until = until
        self._interrupt = interrupt
        self.signals: list[int] = []
        self.waits = 0

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            _eventually(lambda: self._until() or self.signals, "the app to stop")
            if self._interrupt:
                raise KeyboardInterrupt
        return 0

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def kill(self) -> None:  # pragma: no cover - only a stop that hangs
        raise AssertionError("the app server was killed")


@pytest.fixture
def ctx(tmp_path: Path) -> commands.Context:
    identity = Identity(
        repository=tmp_path,
        state_dir=tmp_path / "state",
        checkout=tmp_path,
        root=tmp_path,
        main=True,
        app="shop",
        machine="m",
        git=False,
    )
    return commands.Context(
        identity=identity, environ={}, out=io.StringIO(), err=io.StringIO()
    )


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    """The background processes ``dev run`` started, in order; whatever a failing
    test leaves running is killed."""
    real = process.spawn_piped
    started: list[subprocess.Popen[bytes]] = []

    def record(
        argv: list[str], *, env: dict[str, str], cwd: Path
    ) -> subprocess.Popen[bytes]:
        child = real(argv, env=env, cwd=cwd)
        started.append(child)
        return child

    monkeypatch.setattr(process, "spawn_piped", record)
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
            child.wait()


def _out(ctx: commands.Context) -> str:
    return ctx.out.getvalue()  # type: ignore[attr-defined, no-any-return]


def _err(ctx: commands.Context) -> str:
    return ctx.err.getvalue()  # type: ignore[attr-defined, no-any-return]


def _both_ready(ctx: commands.Context) -> bool:
    out = _out(ctx)
    return "worker | second line" in out and "beat | tick" in out


def _terminated(children: list[subprocess.Popen[bytes]]) -> list[int | None]:
    """How each child ended: ``-SIGTERM`` is the graceful stop."""
    return [child.poll() for child in children]


def test_the_app_exiting_stops_the_worker_and_the_beat_and_their_output_is_prefixed(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _Web(lambda: _both_ready(ctx))
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    env = {**os.environ, "DEV_RUN_PROBE": "from-the-web-env"}
    assert commands._serve(ctx, ["uvicorn"], env, background=_stand_ins()) == 0
    lines = _out(ctx).splitlines()
    worker = [line for line in lines if line.startswith("worker | ")]
    # both streams reach the terminal, each line behind its process's name
    assert "beat | tick" in lines
    assert worker[1] == "worker | second line"
    # the same environment and working directory as the web process
    assert "probe=from-the-web-env" in worker[0]
    child_cwd = worker[0].split("cwd=")[1].split(" unbuffered=")[0]
    assert Path(child_cwd).resolve() == ctx.root.resolve()
    # unbuffered, or a print of the worker would wait in the pipe
    assert worker[0].endswith("unbuffered=1")
    # no orphan: both are reaped, by the graceful stop, and nothing was reported
    assert len(spawned) == 2
    assert _terminated(spawned) == [-signal.SIGTERM, -signal.SIGTERM]
    assert _err(ctx) == ""


def test_ctrl_c_stops_the_app_the_worker_and_the_beat(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _Web(lambda: _both_ready(ctx), interrupt=True)
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    assert commands._serve(ctx, ["uvicorn"], {}, background=_stand_ins()) == 0
    assert web.signals == [signal.SIGINT]
    assert len(spawned) == 2
    assert _terminated(spawned) == [-signal.SIGTERM, -signal.SIGTERM]
    assert _err(ctx) == ""  # a stop is not an early death


def test_sigterm_stops_the_app_the_worker_and_the_beat(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _Web(lambda: False)
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)

    def terminate_dev_run() -> None:
        # only once the background processes run: `_serve` installed its handler
        _eventually(lambda: _both_ready(ctx), "the worker and the beat")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=terminate_dev_run, daemon=True).start()
    assert commands._serve(ctx, ["uvicorn"], {}, background=_stand_ins()) == 0
    assert web.signals == [signal.SIGTERM]  # forwarded to the app server
    assert len(spawned) == 2
    assert _terminated(spawned) == [-signal.SIGTERM, -signal.SIGTERM]
    assert _err(ctx) == ""


def test_a_background_process_that_exits_early_is_reported_and_the_app_keeps_running(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def reported() -> bool:
        if "exited with status" not in _err(ctx):
            return False
        # at the moment the report is out: the beat runs, the app was not signalled
        seen["beat_running"] = spawned[1].poll() is None
        seen["app_signals"] = list(web.signals)
        return True

    web = _Web(reported)
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    background = {
        "worker": [sys.executable, "-c", _EARLY_EXIT],
        "beat": [sys.executable, "-c", _BEAT],
    }
    assert commands._serve(ctx, ["uvicorn"], {}, background=background) == 0
    report = [line for line in _err(ctx).splitlines() if "exited with status" in line]
    assert len(report) == 1
    assert report[0].startswith("warning: worker exited with status 3;")
    assert "uv run sandloom worker" in report[0]
    assert "worker | boom" in _out(ctx).splitlines()  # its last words are on screen
    assert seen == {"beat_running": True, "app_signals": []}
    assert _terminated(spawned)[1] == -signal.SIGTERM  # the beat stopped with the run
    assert "beat exited" not in _err(ctx)


def test_a_background_process_that_cannot_start_is_reported_and_the_other_runs(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _Web(lambda: "beat | tick" in _out(ctx))
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    background = {
        "worker": [str(ctx.root / "no-such-interpreter"), "-c", "pass"],
        "beat": [sys.executable, "-c", _BEAT],
    }
    assert commands._serve(ctx, ["uvicorn"], {}, background=background) == 0
    report = _err(ctx).splitlines()
    assert len(report) == 1
    assert report[0].startswith("warning: worker did not start: ")
    assert len(spawned) == 1  # the beat
    assert _terminated(spawned) == [-signal.SIGTERM]


def test_without_background_processes_only_the_app_runs(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _Web(lambda: True)
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    assert commands._serve(ctx, ["uvicorn"], {}) == 0
    assert commands._serve(ctx, ["uvicorn"], {}, background={}) == 0
    assert spawned == []
    assert _out(ctx) == "" and _err(ctx) == ""


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(
    tmp_path: Path,
) -> None:
    out = io.StringIO()
    deaths: list[tuple[str, int]] = []
    stubborn = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    background = process.Background(
        "worker",
        [sys.executable, "-c", stubborn],
        env=os.environ,
        cwd=tmp_path,
        out=out,
        lock=threading.Lock(),
        died=lambda name, code: deaths.append((name, code)),
    )
    try:
        _eventually(lambda: "worker | ready" in out.getvalue(), "the handler")
        # in a thread: a stop that never kills would wait for this process for ever
        stopper = threading.Thread(
            target=process.stop_all, args=([background],), kwargs={"grace": 0.5}
        )
        stopper.daemon = True
        stopper.start()
        stopper.join(timeout=15)
        assert not stopper.is_alive(), (
            "stop_all waits for a process that ignores SIGTERM"
        )
        assert background.child.returncode == -signal.SIGKILL
    finally:
        if background.child.poll() is None:
            background.child.kill()
            background.child.wait()
    assert deaths == []  # a kill after the grace is still a stop, not a death


def test_bytes_that_are_not_text_do_not_stop_the_output(tmp_path: Path) -> None:
    out = io.StringIO()
    script = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'caf\\xe9\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "print('after', flush=True)\n"
        "time.sleep(300)\n"
    )
    background = process.Background(
        "worker",
        [sys.executable, "-c", script],
        env=os.environ,
        cwd=tmp_path,
        out=out,
        lock=threading.Lock(),
        died=lambda name, code: None,
    )
    try:
        _eventually(lambda: "worker | after" in out.getvalue(), "the line after")
    finally:
        process.stop_all([background])
    assert out.getvalue().splitlines() == ["worker | caf�", "worker | after"]
