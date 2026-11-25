"""The child processes of ``sandloom dev``: ``git``, ``docker``, ``lsof``, the
app server and the background processes of ``dev run`` (a worker and a beat).
One place runs them: a fixed argv (never a shell), the executable resolved on
``PATH`` first."""

from __future__ import annotations

import contextlib
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

#: How long ``dev run`` lets its background processes finish (a worker's
#: running task, say) after it asked them to stop; then they are killed.
STOP_GRACE_SECONDS = 20.0

#: How far the start time ``ps`` reports may be from the one ``dev run``
#: recorded when it started the process (``ps`` counts whole seconds).
START_TOLERANCE_SECONDS = 10.0


def which(name: str) -> str | None:
    return shutil.which(name)


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    capture: bool = True,
    cwd: Path | None = None,
    timeout: float | None = None,
    search_path: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (``argv[0]`` looked up on ``PATH``, ``env``'s with
    ``search_path``; ``FileNotFoundError`` when it is not installed).
    ``capture``: stdout and stderr apart. ``timeout`` raises
    ``subprocess.TimeoutExpired``."""
    exe = (
        shutil.which(argv[0], path=env.get("PATH"))
        if search_path
        else shutil.which(argv[0])
    )
    if exe is None:
        raise FileNotFoundError(f"{argv[0]} is not installed")
    return subprocess.run(  # noqa: S603 - fixed argv built by sandloom dev (git, docker, lsof), no shell
        [exe, *argv[1:]],
        cwd=cwd,
        env=dict(env),
        capture_output=capture,
        text=True,
        check=False,
        timeout=timeout,
    )


def _popen(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, background: bool
) -> subprocess.Popen[bytes]:
    """The one place ``dev run`` starts its own processes. ``background``: a
    process of its own session (a terminal's hang-up and Ctrl-C do not reach
    it), no stdin, and stdout and stderr in one pipe the caller reads."""
    return subprocess.Popen(  # noqa: S603 - sys.executable with arguments built by `sandloom dev run` (uvicorn, worker, beat), no shell
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL if background else None,
        stdout=subprocess.PIPE if background else None,
        stderr=subprocess.STDOUT if background else None,
        start_new_session=background,
    )


def spawn(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
) -> subprocess.Popen[bytes]:
    """Start the app server (``sys.executable -m uvicorn ...``) in the
    foreground of this process."""
    return _popen(argv, env=env, cwd=cwd, background=False)


def spawn_piped(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
) -> subprocess.Popen[bytes]:
    """Start a background process of ``dev run`` (``sys.executable`` with the
    ``sandloom worker`` / ``sandloom beat`` entry point), its output in a pipe.

    It has a session of its own, so a terminal that closes does not hang it up
    directly: a Celery worker whose output is a pipe turns SIGHUP into a
    restart on Linux, and the new image would outlive ``dev run``. ``dev run``
    receives the hang-up and Ctrl-C itself and stops the process
    (:func:`stop_all`)."""
    return _popen(argv, env=env, cwd=cwd, background=True)


def started_at(pid: int, env: Mapping[str, str]) -> float | None:
    """When process ``pid`` started (epoch seconds, whole seconds), as ``ps``
    reports it; ``None`` without such a process, or when ``ps`` is missing or
    says something unreadable."""
    try:
        done = run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            env={**env, "LC_ALL": "C"},
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return time.mktime(time.strptime(done.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


class Background:
    """One background process of ``dev run`` (the worker or the beat): its
    output reaches ``out`` line by line behind ``<name> |``, and ``died`` hears
    of an exit nobody asked for.

    Two daemon threads serve it: one copies the output, one waits for the exit
    (a grandchild that inherited the pipe can keep the output open after the
    process is gone, so the exit is not read off the end of the output)."""

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        out: TextIO,
        lock: threading.Lock,
        died: Callable[[str, int], None],
    ) -> None:
        self.name = name
        self.child = spawn_piped(argv, env=env, cwd=cwd)
        #: Just after the start, for the run marker (:func:`started_at` checks it).
        self.started = time.time()
        stream = self.child.stdout
        if stream is None:
            raise RuntimeError(f"{name}: spawn_piped gave no output pipe")
        self._stream = stream
        self._out = out
        self._lock = lock
        self._died = died
        self._stopping = threading.Event()
        self._copier = threading.Thread(
            target=self._copy, name=f"dev-run-{name}-output", daemon=True
        )
        self._watcher = threading.Thread(
            target=self._watch, name=f"dev-run-{name}-exit", daemon=True
        )
        self._copier.start()
        self._watcher.start()

    def _copy(self) -> None:
        for raw in self._stream:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            try:
                with self._lock:
                    self._out.write(f"{self.name} | {line}\n")
                    self._out.flush()
            except (OSError, UnicodeError):
                # A terminal that went away (EPIPE) or cannot show this line
                # (an encoding it lacks): the line is lost, but the pipe is
                # still read, or the process would block once it filled.
                continue

    def _watch(self) -> None:
        code = self.child.wait()
        # Its last lines first: the copier may still be relaying them.
        self._copier.join(timeout=1.0)
        if not self._stopping.is_set():
            with self._lock:
                self._died(self.name, code)

    def terminate(self) -> None:
        """Ask it to stop (SIGTERM: a worker finishes its running tasks); what
        follows is no longer an early death."""
        self._stopping.set()
        if self.child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.child.send_signal(signal.SIGTERM)

    def finish(self, deadline: float) -> None:
        """Wait for the exit until ``deadline`` (``time.monotonic``), then kill.
        A second Ctrl-C while it waits is no more waiting: kill."""
        try:
            self.child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            self.child.kill()
            self.child.wait()
        self._copier.join(timeout=1.0)
        if not self._copier.is_alive():
            self._stream.close()


def stop_all(
    background: Sequence[Background],
    *,
    grace: float = STOP_GRACE_SECONDS,
    deadline: float | None = None,
) -> None:
    """Stop every process of ``background`` together: all are asked first, then
    each is waited for under one deadline and killed past it. Nothing of
    ``dev run`` outlives it. ``deadline`` (``time.monotonic``) is the one the
    caller already started counting from; without it ``grace`` seconds from
    now."""
    for process in background:
        process.terminate()
    end = time.monotonic() + grace if deadline is None else deadline
    for process in background:
        process.finish(end)


__all__ = [
    "START_TOLERANCE_SECONDS",
    "STOP_GRACE_SECONDS",
    "Background",
    "run",
    "spawn",
    "spawn_piped",
    "started_at",
    "stop_all",
    "which",
]
