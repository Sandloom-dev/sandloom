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


def spawn(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
) -> subprocess.Popen[bytes]:
    """Start the app server (``sys.executable -m uvicorn ...``) in the
    foreground of this process."""
    return subprocess.Popen(list(argv), cwd=cwd, env=dict(env))  # noqa: S603 - sys.executable -m uvicorn with arguments built by `sandloom dev run`, no shell


def spawn_piped(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
) -> subprocess.Popen[bytes]:
    """Start a background process of ``dev run`` (``sys.executable`` with the
    ``sandloom worker`` / ``sandloom beat`` entry point): stdout and stderr in
    one pipe the caller reads, no stdin. It stays in this process's group, so a
    terminal that closes reaches it as it reaches the app server."""
    return subprocess.Popen(  # noqa: S603 - sys.executable with arguments built by `sandloom dev run`, no shell
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


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
            with self._lock:
                self._out.write(f"{self.name} | {line}\n")
                self._out.flush()

    def _watch(self) -> None:
        code = self.child.wait()
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
        """Wait for the exit until ``deadline`` (``time.monotonic``), then kill."""
        try:
            self.child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        self._copier.join(timeout=1.0)
        if not self._copier.is_alive():
            self._stream.close()


def stop_all(
    background: Sequence[Background], *, grace: float = STOP_GRACE_SECONDS
) -> None:
    """Stop every process of ``background`` together: all are asked first, then
    each is waited for under one deadline and killed past it. Nothing of
    ``dev run`` outlives it."""
    for process in background:
        process.terminate()
    deadline = time.monotonic() + grace
    for process in background:
        process.finish(deadline)


__all__ = [
    "STOP_GRACE_SECONDS",
    "Background",
    "run",
    "spawn",
    "spawn_piped",
    "stop_all",
    "which",
]
