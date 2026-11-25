"""``sandloom dev run`` beside its worker and beat: the processes it starts, their
prefixed output, an early exit, and that nothing outlives the run, not even a run
that is killed (the next one stops what it left).

The app server is a stand-in (a fake child) and the worker and the beat are real
short Python processes, so what is proved about the stop is proved on real pids:
each test ends with them reaped and with the status the stop gave them."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import socket
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


def _dead_pid() -> int:
    """The pid of a process that ran and was reaped."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


class _Web:
    """The app server child: its first ``wait`` returns once ``until`` holds or
    it was sent a signal (raising ``KeyboardInterrupt`` first when ``interrupt``,
    as Ctrl-C does); a later ``wait`` returns at once, as after a stop. ``pid``:
    what the run marker records for it (a test that reads the marker gives a
    dead one)."""

    def __init__(
        self, until: Callable[[], object], *, interrupt: bool = False, pid: int = 0
    ) -> None:
        self._until = until
        self._interrupt = interrupt
        self.pid = pid
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
    assert isinstance(ctx.out, io.StringIO)
    return ctx.out.getvalue()


def _err(ctx: commands.Context) -> str:
    assert isinstance(ctx.err, io.StringIO)
    return ctx.err.getvalue()


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


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_a_hang_up_stop_the_app_the_worker_and_the_beat(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
    signum: signal.Signals,
) -> None:
    """A closing terminal sends SIGHUP: it stops everything like SIGTERM, with
    the app server asked in the same way (SIGTERM, its own clean shutdown)."""
    web = _Web(lambda: False)
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)

    def idle(_signum: int, _frame: object) -> None:
        return None

    # The test's own handler, so that a `_serve` that installs none leaves a
    # failing test and not a pytest process that the signal ended.
    before = signal.signal(signum, idle)

    def signal_dev_run() -> None:
        # only once the background processes run: `_serve` installed its handler
        _eventually(lambda: _both_ready(ctx), "the worker and the beat")
        os.kill(os.getpid(), signum)

    try:
        threading.Thread(target=signal_dev_run, daemon=True).start()
        assert commands._serve(ctx, ["uvicorn"], {}, background=_stand_ins()) == 0
        assert signal.getsignal(signum) is idle  # the handler it replaced is back
    finally:
        signal.signal(signum, before)
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


def test_a_failure_other_than_the_system_refusing_stops_the_app_and_the_started_ones(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an ``OSError`` is reported and survived: anything else is ours, it
    raises, and neither the app server nor the worker already started stays."""
    web = _Web(lambda: True)
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    recording = process.spawn_piped

    def refuse_the_beat(
        argv: list[str], *, env: dict[str, str], cwd: Path
    ) -> subprocess.Popen[bytes]:
        if "refuse" in argv:
            raise ValueError("not an OSError")
        return recording(argv, env=env, cwd=cwd)

    monkeypatch.setattr(process, "spawn_piped", refuse_the_beat)
    background = {
        "worker": [sys.executable, "-c", _WORKER],
        "beat": [sys.executable, "-c", _BEAT, "refuse"],
    }
    with pytest.raises(ValueError, match="not an OSError"):
        commands._serve(ctx, ["uvicorn"], {}, background=background)
    assert web.signals == [signal.SIGINT]
    assert len(spawned) == 1  # the worker
    assert _terminated(spawned) == [-signal.SIGTERM]
    assert _err(ctx) == ""


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


def _background(
    tmp_path: Path,
    script: str,
    *,
    out: io.StringIO | None = None,
    died: Callable[[str, int], None] = lambda name, code: None,
) -> process.Background:
    return process.Background(
        "worker",
        [sys.executable, "-c", script],
        env=os.environ,
        cwd=tmp_path,
        out=out if out is not None else io.StringIO(),
        lock=threading.Lock(),
        died=died,
    )


_STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(300)\n"
)


def test_a_background_process_has_a_session_of_its_own_and_the_app_does_not(
    tmp_path: Path,
) -> None:
    """A terminal's hang-up reaches the app server's process group, never a
    worker (Celery turns a hang-up with piped output into a restart whose new
    image would outlive `dev run`): `dev run` gets it and stops the worker."""
    background = _background(tmp_path, _BEAT)
    app = process.spawn(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        cwd=tmp_path,
        env=os.environ,
    )
    try:
        pid = background.child.pid
        assert os.getsid(pid) == pid != os.getsid(0)
        assert os.getsid(app.pid) == os.getsid(0)
    finally:
        app.kill()
        app.wait()
        process.stop_all([background])


def test_a_keyboard_interrupt_while_waiting_for_a_process_kills_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second Ctrl-C during the grace is no more waiting: the process that
    ignores SIGTERM is killed at once, not after the whole grace."""
    out = io.StringIO()
    background = _background(tmp_path, _STUBBORN, out=out)
    real_wait = background.child.wait
    calls: list[float | None] = []

    def interrupted(timeout: float | None = None) -> int:
        calls.append(timeout)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_wait(timeout)

    try:
        _eventually(lambda: "worker | ready" in out.getvalue(), "the handler")
        monkeypatch.setattr(background.child, "wait", interrupted)
        background.terminate()
        started = time.monotonic()
        try:
            background.finish(started + 120.0)
        except KeyboardInterrupt:  # not pytest's own: it would end the session
            pytest.fail("finish let a second Ctrl-C through")
        assert time.monotonic() - started < 60.0
        assert background.child.returncode == -signal.SIGKILL
    finally:
        if background.child.poll() is None:
            background.child.kill()
            background.child.wait()


class _Hanging:
    """An app server that does not stop: its ``wait`` runs out the timeout it
    is given, and once killed it is gone (the first ``wait`` is Ctrl-C)."""

    pid = 0

    def __init__(self, until: Callable[[], object]) -> None:
        self._until = until
        self.killed = False
        self.signals: list[int] = []
        self.waits = 0
        self.interrupted_at = 0.0

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            _eventually(self._until, "the worker")
            self.interrupted_at = time.monotonic()
            raise KeyboardInterrupt
        if self.killed:
            return -signal.SIGKILL
        time.sleep(timeout or 0.0)
        raise subprocess.TimeoutExpired("uvicorn", timeout or 0.0)

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def kill(self) -> None:
        self.killed = True


def test_ctrl_c_gives_the_app_and_the_background_processes_one_grace_between_them(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app server that ignores the interrupt and a worker that ignores
    SIGTERM wait out one grace period together, not one after the other."""
    grace = 1.5
    monkeypatch.setattr(process, "STOP_GRACE_SECONDS", grace)
    web = _Hanging(lambda: "worker | ready" in _out(ctx))
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    background = {"worker": [sys.executable, "-c", _STUBBORN]}
    assert commands._serve(ctx, ["uvicorn"], {}, background=background) == 0
    elapsed = time.monotonic() - web.interrupted_at
    assert web.killed and web.signals == [signal.SIGINT]
    assert _terminated(spawned) == [-signal.SIGKILL]
    # one grace, not two (the interrupt waits for the app, then the stop for the worker)
    assert grace <= elapsed < 1.6 * grace, elapsed


class _InterruptedTwice(_Hanging):
    """An app server whose stop is interrupted too: the stop's own ``wait`` is
    a second Ctrl-C."""

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return -signal.SIGKILL if self.killed else self._second_ctrl_c()

    def _second_ctrl_c(self) -> int:
        raise KeyboardInterrupt


def test_the_app_server_that_is_interrupted_while_it_is_stopped_is_killed() -> None:
    web = _InterruptedTwice(lambda: True)
    with pytest.raises(KeyboardInterrupt):
        web.wait()  # the run's own wait, as in `_serve`
    try:
        commands._stop_app(web, time.monotonic() + 60)  # type: ignore[arg-type]
    except KeyboardInterrupt:  # not pytest's own: it would end the session
        pytest.fail("the stop let a second Ctrl-C through")
    assert web.killed and web.signals == [signal.SIGINT]


def test_stop_all_waits_until_the_deadline_it_is_given_and_no_longer(
    tmp_path: Path,
) -> None:
    outs = [io.StringIO(), io.StringIO()]
    stubborn = [_background(tmp_path, _STUBBORN, out=out) for out in outs]
    try:
        for out in outs:
            _eventually(
                lambda out=out: "worker | ready" in out.getvalue(), "the handler"
            )
        # an overdue deadline: both are killed with no waiting, whatever the grace
        stopper = threading.Thread(
            target=process.stop_all,
            args=(stubborn,),
            kwargs={"grace": 120.0, "deadline": time.monotonic() - 1.0},
            daemon=True,
        )
        stopper.start()
        stopper.join(timeout=30)
        assert not stopper.is_alive(), "stop_all ignored the deadline it was given"
        assert [each.child.returncode for each in stubborn] == [
            -signal.SIGKILL,
            -signal.SIGKILL,
        ]
    finally:
        for each in stubborn:
            if each.child.poll() is None:
                each.child.kill()
                each.child.wait()


class _Writes(io.StringIO):
    """An output that refuses some lines: ``fail`` raises for a line that holds
    the text it is mapped to."""

    def __init__(self, fail: dict[str, BaseException]) -> None:
        super().__init__()
        self._fail = fail

    def write(self, text: str) -> int:
        for marker, error in self._fail.items():
            if marker in text:
                raise error
        return super().write(text)


_FOUR_LINES = (
    "import time\n"
    "for word in ('bad-one', 'bad-two', 'good-one', 'good-two'):\n"
    "    print(word, flush=True)\n"
    "time.sleep(300)\n"
)


def test_an_output_that_refuses_a_line_does_not_stop_the_relay(
    tmp_path: Path,
) -> None:
    """A terminal that went away (`OSError`) or cannot encode a line
    (`UnicodeError`) loses that line only: the pipe is still read, or the worker
    would block once it filled."""
    out = _Writes(
        {
            "bad-one": BrokenPipeError(32, "Broken pipe"),
            "bad-two": UnicodeEncodeError("ascii", "bad-two", 0, 1, "ordinal"),
        }
    )
    background = _background(tmp_path, _FOUR_LINES, out=out)
    try:
        _eventually(lambda: "worker | good-two" in out.getvalue(), "the good lines")
    finally:
        process.stop_all([background])
    assert out.getvalue().splitlines() == ["worker | good-one", "worker | good-two"]


def test_an_error_of_another_kind_is_not_swallowed_by_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the two losses above are survived: anything else is a bug of ours
    and reaches the thread's exception hook."""
    raised: list[BaseException] = []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: raised.append(args.exc_value)
    )
    out = _Writes({"bad-one": RuntimeError("a bug of ours")})
    background = _background(tmp_path, _FOUR_LINES, out=out)
    try:
        _eventually(lambda: raised, "the relay's exception")
    finally:
        process.stop_all([background])
    assert [str(error) for error in raised] == ["a bug of ours"]


class _SlowLock:
    """A lock that is slow to hand itself over: the relay of a line is slower
    than the process's exit (as with a busy terminal)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> bool:
        time.sleep(0.3)
        return self._lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def test_an_exit_is_reported_after_the_last_lines_of_the_process(
    tmp_path: Path,
) -> None:
    """The worker's last words explain its death, so they come first: the
    report waits for the relay of what was written before the exit."""
    out = io.StringIO()
    seen: list[str] = []
    script = (
        "import sys\n"
        "print('first', flush=True)\n"
        "print('last', flush=True)\n"
        "sys.exit(3)\n"
    )
    background = process.Background(
        "worker",
        [sys.executable, "-c", script],
        env=os.environ,
        cwd=tmp_path,
        out=out,
        lock=_SlowLock(),  # type: ignore[arg-type]
        died=lambda name, code: seen.append(out.getvalue()),
    )
    try:
        _eventually(lambda: seen, "the report of the exit")
    finally:
        process.stop_all([background])
    assert seen == ["worker | first\nworker | last\n"]


def test_started_at_reads_when_a_process_started_and_is_none_for_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = time.time()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        started = process.started_at(child.pid, os.environ)
        assert started is not None
        assert before - 5 <= started <= time.time() + 1
    finally:
        child.kill()
        child.wait()
    assert process.started_at(child.pid, os.environ) is None  # gone
    # `ps` that says something unreadable, or cannot run: no answer, no crash
    monkeypatch.setattr(
        process,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, "not a date\n", ""
        ),
    )
    assert process.started_at(1, os.environ) is None

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("ps is not installed")

    monkeypatch.setattr(process, "run", missing)
    assert process.started_at(1, os.environ) is None


# --------------------------------------------------------------------------- #
# The processes of a run that was killed
# --------------------------------------------------------------------------- #


def test_the_run_marker_records_the_processes_while_they_run(
    ctx: commands.Context,
    spawned: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = commands.run_file(ctx.root)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
    recorded_pids: dict[str, int] = {}
    live: list[tuple[dict[str, int], list[str]]] = []

    def recorded() -> bool:
        children = commands._recorded_children(ctx.root)
        if set(children) != {"web", "worker", "beat"}:
            return False
        recorded_pids.update({name: pid for name, (pid, _) in children.items()})
        live.append(commands._live_recorded(ctx))
        return True

    web = _Web(recorded, pid=_dead_pid())
    monkeypatch.setattr(process, "spawn", lambda argv, env, cwd: web)
    assert (
        commands._serve(ctx, ["uvicorn"], {}, background=_stand_ins(), marker=marker)
        == 0
    )
    assert recorded_pids == {
        "web": web.pid,
        "worker": spawned[0].pid,
        "beat": spawned[1].pid,
    }
    # what the next run checks: `ps` agrees with the start recorded for each one
    # that runs (the app server's stand-in does not)
    assert live == [({"worker": spawned[0].pid, "beat": spawned[1].pid}, [])]
    assert json.loads(marker.read_text())["pid"] == os.getpid()  # still the run's


@pytest.fixture
def sleepers() -> Iterator[Callable[..., subprocess.Popen[bytes]]]:
    """Start real processes that stand for a killed run's leftovers. A thread
    reaps each (a stopped child stays a zombie, which `pid_alive` still counts
    as running, until it is waited for); what is left is killed at the end."""
    made: list[subprocess.Popen[bytes]] = []

    def start(*, ignore_sigterm: bool = False) -> subprocess.Popen[bytes]:
        script = (
            _STUBBORN if ignore_sigterm else _STUBBORN.replace("SIG_IGN", "SIG_DFL")
        )
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        threading.Thread(target=child.wait, daemon=True).start()
        made.append(child)
        return child

    yield start
    for child in made:
        if child.poll() is None:
            child.kill()
        _eventually(lambda child=child: child.poll() is not None, "the reaping")
        if child.stdout is not None:
            child.stdout.close()


def _started(child: subprocess.Popen[bytes]) -> float:
    started = process.started_at(child.pid, os.environ)
    assert started is not None
    return started


def _write_marker(
    ctx: commands.Context,
    children: dict[str, tuple[int, float]],
    *,
    host: str | None = None,
) -> None:
    """The marker of a run that is gone (its own pid ended)."""
    marker = commands.run_file(ctx.root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "pid": _dead_pid(),
                "host": host or socket.gethostname(),
                "children": {
                    name: {"pid": pid, "started": started}
                    for name, (pid, started) in children.items()
                },
            }
        )
    )


def test_what_a_killed_run_left_is_stopped_and_named_in_one_line(
    ctx: commands.Context, sleepers: Callable[..., subprocess.Popen[bytes]]
) -> None:
    worker, beat = sleepers(), sleepers()
    _write_marker(
        ctx,
        {
            "web": (_dead_pid(), time.time()),
            "worker": (worker.pid, _started(worker)),
            "beat": (beat.pid, _started(beat)),
        },
    )
    commands.stop_leftovers(ctx)
    _eventually(
        lambda: worker.poll() is not None and beat.poll() is not None, "the exits"
    )
    assert (worker.returncode, beat.returncode) == (-signal.SIGTERM, -signal.SIGTERM)
    lines = _err(ctx).splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(
        "warning: a previous dev run did not shut down; stopped what it left running: "
    )
    assert f"worker (pid {worker.pid})" in lines[0]
    assert f"beat (pid {beat.pid})" in lines[0]
    assert "web" not in lines[0]  # that one was gone already


def test_a_pid_that_another_process_took_is_left_alone(
    ctx: commands.Context, sleepers: Callable[..., subprocess.Popen[bytes]]
) -> None:
    """The system hands a pid to another process once it is free: one that
    started at another time than the marker says is not ours to stop."""
    stranger = sleepers()
    _write_marker(ctx, {"worker": (stranger.pid, _started(stranger) - 3600)})
    commands.stop_leftovers(ctx)
    assert stranger.poll() is None
    assert _err(ctx) == ""


def test_a_leftover_whose_start_cannot_be_read_is_named_and_left_running(
    ctx: commands.Context,
    sleepers: Callable[..., subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    survivor = sleepers()
    _write_marker(ctx, {"beat": (survivor.pid, time.time())})
    monkeypatch.setattr(process, "started_at", lambda pid, env: None)
    commands.stop_leftovers(ctx)
    assert survivor.poll() is None
    lines = _err(ctx).splitlines()
    assert len(lines) == 1
    assert f"beat (pid {survivor.pid})" in lines[0] and "left alone" in lines[0]


def test_the_marker_of_another_host_names_nothing_to_stop(
    ctx: commands.Context, sleepers: Callable[..., subprocess.Popen[bytes]]
) -> None:
    other = sleepers()
    _write_marker(ctx, {"worker": (other.pid, _started(other))}, host="another-host")
    commands.stop_leftovers(ctx)
    assert other.poll() is None
    assert _err(ctx) == ""


def test_a_leftover_that_ignores_sigterm_is_killed_after_the_wait(
    ctx: commands.Context,
    sleepers: Callable[..., subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(commands, "LEFTOVER_STOP_SECONDS", 0.5)
    stubborn = sleepers(ignore_sigterm=True)
    _write_marker(ctx, {"worker": (stubborn.pid, _started(stubborn))})
    commands.stop_leftovers(ctx)
    _eventually(lambda: stubborn.poll() is not None, "the kill")
    assert stubborn.returncode == -signal.SIGKILL


@pytest.mark.parametrize(
    "children",
    [
        None,  # a marker from before the children were recorded
        "worker",
        {"worker": "not-an-entry"},
        {"worker": {"pid": "7", "started": 1.0}},
        {"worker": {"pid": True, "started": 1.0}},  # a bool is no pid (1)
        {"worker": {"pid": 7}},
        {"worker": {"pid": 7, "started": "yesterday"}},
    ],
)
def test_a_marker_without_usable_children_names_none(
    ctx: commands.Context, children: object
) -> None:
    marker = commands.run_file(ctx.root)
    marker.parent.mkdir(parents=True)
    record: dict[str, object] = {"pid": _dead_pid(), "host": socket.gethostname()}
    if children is not None:
        record["children"] = children
    marker.write_text(json.dumps(record))
    assert commands._recorded_children(ctx.root) == {}
    commands.stop_leftovers(ctx)
    assert _err(ctx) == ""


def test_an_unreadable_marker_names_no_children(ctx: commands.Context) -> None:
    assert commands._recorded_children(ctx.root) == {}  # none at all
    marker = commands.run_file(ctx.root)
    marker.parent.mkdir(parents=True)
    marker.write_text("{not json")
    assert commands._recorded_children(ctx.root) == {}
    marker.write_text("[1, 2]")
    assert commands._recorded_children(ctx.root) == {}


#: A whole ``dev run`` in a process of its own, serving sleeping stand-ins.
_RUN_TO_KILL = (
    "import io, json, os, socket, sys\n"
    "from pathlib import Path\n"
    "from sandloom.core.cli.dev import commands\n"
    "from sandloom.core.cli.dev.identity import Identity\n"
    "root = Path(sys.argv[1])\n"
    "identity = Identity(repository=root, state_dir=root / 'state', checkout=root,"
    " root=root, main=True, app='shop', machine='m', git=False)\n"
    "ctx = commands.Context(identity=identity, environ={}, out=io.StringIO(),"
    " err=io.StringIO())\n"
    "marker = commands.run_file(root)\n"
    "marker.parent.mkdir(parents=True, exist_ok=True)\n"
    "marker.write_text(json.dumps({'pid': os.getpid(), 'host': socket.gethostname()}))\n"
    "sleeper = [sys.executable, '-c', 'import time; time.sleep(300)']\n"
    "commands._serve(ctx, sleeper, dict(os.environ),"
    " background={'worker': sleeper, 'beat': sleeper}, marker=marker)\n"
)


def test_the_next_run_stops_what_a_killed_run_left_running(
    ctx: commands.Context,
) -> None:
    """The scenario of the finding: SIGKILL of `dev run` runs no `finally`, and
    the app server, the worker and the beat go on."""
    run = subprocess.Popen([sys.executable, "-c", _RUN_TO_KILL, str(ctx.root)])
    pids: list[int] = []
    try:
        _eventually(
            lambda: len(commands._recorded_children(ctx.root)) == 3,
            "the run's three processes",
        )
        pids = [pid for pid, _ in commands._recorded_children(ctx.root).values()]
        run.kill()
        run.wait()
        assert all(commands.pid_alive(pid) for pid in pids)  # they stayed
        commands.stop_leftovers(ctx)
        _eventually(
            lambda: not any(commands.pid_alive(pid) for pid in pids), "their exit"
        )
        assert len(_err(ctx).splitlines()) == 1
    finally:
        if run.poll() is None:
            run.kill()
            run.wait()
        for pid in pids:
            if commands.pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


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
