"""File locks with a named holder: wait while it is held, say who holds it.

Three locks, always taken in this order and never the other way (so two
commands cannot deadlock): the **machine** lock (``~/.sandloom/ports.lock``,
choosing ports), the **repository** lock (``<state>/dev.lock``) and the
**checkout** locks (``<project>/.sandloom/checkout.lock``; several sorted by
path).

The lock is ``fcntl.flock`` (``msvcrt.locking`` on Windows) on the file; the
file also records its holder (pid, host, command, a heartbeat refreshed every
few seconds). A waiter prints who it waits for and waits as long as that
holder keeps the lock: there is no fixed timeout.

The lock itself is the only truth: the operating system releases ``flock`` /
``msvcrt`` when the holder's last open descriptor closes, so a holder that
died frees the lock without help. A record that names a process which looks
gone while the lock is still taken means some live process still holds the
descriptor (a child that inherited it, a pid namespace this host cannot see):
the waiter keeps waiting and never takes such a lock over. Nothing ever
unlinks a lock file, so every process locks the same inode.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

if sys.platform == "win32":  # pragma: no cover - Windows only
    import ctypes
    import msvcrt
else:
    import fcntl

#: Seconds between heartbeats while a lock is held.
HEARTBEAT_SECONDS = 5.0


def pid_alive(pid: int) -> bool:
    """Whether a process ``pid`` exists on this host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows only
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def read_holder(path: Path) -> dict[str, Any]:
    """The holder record of ``path`` (``{}``: none readable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def describe(holder: dict[str, Any]) -> str:
    if not holder:
        return "another process"
    command = holder.get("command") or "a sandloom command"
    return f"{command} (pid {holder.get('pid')} on {holder.get('host')})"


class FileLock:
    """An exclusive lock on ``path`` (see the module doc); a context manager."""

    def __init__(
        self,
        path: Path,
        *,
        command: str = "",
        waiting: str | None = None,
        out: TextIO | None = None,
        poll: float = 0.2,
        timeout: float | None = None,
    ) -> None:
        self.path = path
        self.command = command or " ".join(["sandloom", *sys.argv[1:3]])
        self.waiting = waiting
        self.out = out if out is not None else sys.stderr
        self.poll = poll
        self.timeout = timeout
        self._fd: int | None = None
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None

    # ------------------------------------------------------------- record
    def _record(self) -> dict[str, Any]:
        now = time.time()
        return {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "command": self.command,
            "started": getattr(self, "_started", now),
            "heartbeat": now,
        }

    def _write_record(self) -> None:
        if self._fd is None:
            return
        data = json.dumps(self._record()).encode()
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, data)

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self._write_record()

    # ------------------------------------------------------------ acquire
    def acquire(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        announced = False
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            while not _try_lock(fd):
                holder = read_holder(self.path)
                if not announced:
                    what = self.waiting or f"waiting for {describe(holder)}"
                    print(f"{what} ({self.path})", file=self.out, flush=True)
                    announced = True
                if (
                    self.timeout is not None
                    and time.monotonic() - started > self.timeout
                ):
                    raise TimeoutError(f"{self.path} is held by {describe(holder)}")
                time.sleep(self.poll)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._started = time.time()
        self._write_record()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)
        self._beat.start()
        return self

    def release(self) -> None:
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=1)
            self._beat = None
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, 0)
            with contextlib.suppress(OSError):
                _unlock(self._fd)
            os.close(self._fd)
            self._fd = None
        self._stop = threading.Event()

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


__all__ = [
    "HEARTBEAT_SECONDS",
    "FileLock",
    "describe",
    "pid_alive",
    "read_holder",
]
