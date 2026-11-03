"""Who and where a run is: the machine id, the checkout root, short hashes.

Shared by the test harness (owners of test resources, test-run slots) and
``sandloom dev`` (compose project names, per-checkout databases). Paths alone
do not identify a run: cloud sandboxes share paths like ``/workspace/repo``,
so owners and derived names also carry a machine id, generated once into
``~/.sandloom/machine-id`` (``SANDLOOM_HOME`` moves that directory).

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - Windows only
    import msvcrt
else:
    import fcntl

#: Overrides ``~/.sandloom`` (the machine id, the port registry, its lock).
HOME_ENV = "SANDLOOM_HOME"
MACHINE_ID_FILE = "machine-id"


def sandloom_home(environ: Mapping[str, str] | None = None) -> Path:
    """``$SANDLOOM_HOME``, else ``~/.sandloom``."""
    env = os.environ if environ is None else environ
    explicit = (env.get(HOME_ENV) or "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".sandloom"


def _valid(text: str) -> bool:
    return len(text) == 12 and all(c in "0123456789abcdef" for c in text)


def _read_id(path: Path) -> str | None:
    """The id stored in ``path``; ``None`` when missing, empty or garbage."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if _valid(text) else None


def _written(directory: Path, value: str) -> Path:
    """A complete temporary file holding ``value`` in ``directory``."""
    temp = directory / f".{MACHINE_ID_FILE}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    with open(temp, "x", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return temp


@contextlib.contextmanager
def _repair_lock(directory: Path) -> Iterator[None]:
    """An exclusive lock beside the id while a broken file is repaired."""
    fd = os.open(directory / f"{MACHINE_ID_FILE}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def machine_id(environ: Mapping[str, str] | None = None) -> str:
    """This machine's id (12 hex), created on first use.

    Created atomically and exclusively: a complete temporary file is
    hard-linked into place, so a reader never sees a partial file and two
    processes racing on a new machine agree on the one whose link won. An
    empty or unreadable file (a crash of an older version, a disk that
    filled up) is repaired once, under a lock beside it.
    """
    path = sandloom_home(environ) / MACHINE_ID_FILE
    found = _read_id(path)
    if found is not None:
        return found
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _written(path.parent, uuid.uuid4().hex[:12])
    try:
        try:
            os.link(temp, path)
        except OSError:  # it exists (or no hard links here): read or repair it
            pass
        found = _read_id(path)
        if found is not None:
            return found
        with _repair_lock(path.parent):
            found = _read_id(path)  # another process may have repaired it
            if found is not None:
                return found
            os.replace(temp, path)
        found = _read_id(path)
        if found is None:
            raise OSError(f"cannot store the machine id in {path}")
        return found
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)


def canonical(path: Path) -> str:
    """``realpath`` of ``path`` as a hash input (case-folded on Windows)."""
    text = os.path.realpath(path)
    return text.casefold() if sys.platform == "win32" else text


def short_hash(*parts: str, length: int = 6) -> str:
    """The first ``length`` hex digits of sha256 over ``parts``."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def checkout_root(cwd: Path | None = None) -> Path:
    """The nearest directory holding ``.git`` (a dir, or a worktree's file)
    above ``cwd``, else ``cwd`` itself (resolved)."""
    start = (cwd or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


__all__ = [
    "HOME_ENV",
    "canonical",
    "checkout_root",
    "machine_id",
    "sandloom_home",
    "short_hash",
]
