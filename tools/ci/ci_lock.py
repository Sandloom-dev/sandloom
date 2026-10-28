"""The one CPU-quiet lock shared by local CI and CPU benchmarks.

``/tmp/sandloom-ci-local.lock`` is held by ``tools/ci/ci_local.py`` for a whole run
and by CPU benchmarks for their measurement; each waits for (or refuses to
run beside) the other. Stdlib only, so benchmarks import it from any venv.

Protocol: the lock is ``fcntl.flock(LOCK_EX)`` on the file — never the file's
existence. The kernel drops a flock when its holder dies, so a crashed holder
(even ``kill -9``) never blocks anyone. The holder writes ``key=value`` lines
(``protocol=flock``, ``pid``, ``run_id``, ``ref``, …) for humans and unlinks
the file on release, still holding the flock; an acquirer that locked an
inode which was meanwhile unlinked or replaced retries on the current path.

A leftover file — empty (an old benchmark that only flocked it), unparseable,
or naming a dead pid — is stale whenever its flock is free, and is taken over.
For the transition from the existence-based protocol, a file WITHOUT
``protocol=flock`` that names a live pid counts as held (an older ci_local in
another checkout holds it without flock).

    lock = ci_lock.try_acquire({"tool": "http_pipeline"})   # None if busy
    lock = ci_lock.acquire({...}, on_wait=print)             # blocks, polls
    ci_lock.is_held()                                        # probe only
    lock.release()
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

LOCK_FILE = Path("/tmp/sandloom-ci-local.lock")
PROTOCOL = "flock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _legacy_holder(text: str) -> bool:
    """A pre-flock holder: no ``protocol=`` line, a live pid that is not us."""
    if re.search(r"^protocol=", text, re.M):
        return False
    m = re.search(r"^pid=(\d+)$", text, re.M)
    if m is None:
        return False
    pid = int(m.group(1))
    return pid != os.getpid() and _pid_alive(pid)


def _read_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def _same_inode(fd: int, path: Path) -> bool:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    own = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (own.st_dev, own.st_ino)


def holder_text(path: Path = LOCK_FILE) -> str:
    """What the current (or last) holder wrote; ``""`` if there is no file."""
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""


class Lock:
    """A held lock; ``release()`` is idempotent."""

    def __init__(self, fd: int, path: Path) -> None:
        self.fd: int | None = fd
        self.path = path

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            # unlink while still holding the flock: a waiter on this inode
            # then sees it detached and retries on the (new) path
            if _same_inode(fd, self.path):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
        finally:
            os.close(fd)  # drops the flock

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def try_acquire(info: Mapping[str, object], path: Path = LOCK_FILE) -> Lock | None:
    """Take the lock now, or return ``None`` if someone holds it."""
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        if not _same_inode(fd, path):  # released (unlinked) under us: retry
            os.close(fd)
            continue
        if _legacy_holder(_read_fd(fd)):
            os.close(fd)
            return None
        lines = {"protocol": PROTOCOL, "pid": os.getpid(), **info}
        data = "".join(f"{k}={v}\n" for k, v in lines.items()).encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)
        return Lock(fd, path)


def acquire(
    info: Mapping[str, object],
    path: Path = LOCK_FILE,
    *,
    on_wait: Callable[[str], None] | None = None,
    poll_s: float = 5.0,
    announce_s: float = 60.0,
) -> Lock:
    """Block until the lock is ours; ``on_wait(holder_text)`` every ``announce_s``."""
    announced: float | None = None
    while (lock := try_acquire(info, path)) is None:
        if on_wait and (announced is None or time.monotonic() - announced > announce_s):
            on_wait(holder_text(path))
            announced = time.monotonic()
        time.sleep(poll_s)
    return lock


def is_held(path: Path = LOCK_FILE) -> bool:
    """Probe without taking the lock (never creates the file)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return _legacy_holder(_read_fd(fd))
    finally:
        os.close(fd)


@contextlib.contextmanager
def held(info: Mapping[str, object], path: Path = LOCK_FILE) -> Iterator[Lock | None]:
    """``with held({...}) as lock:`` — ``lock`` is ``None`` if it was busy."""
    lock = try_acquire(info, path)
    try:
        yield lock
    finally:
        if lock is not None:
            lock.release()
