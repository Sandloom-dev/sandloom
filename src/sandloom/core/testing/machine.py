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
from collections.abc import Mapping
from pathlib import Path

#: Overrides ``~/.sandloom`` (the machine id, the port registry, its lock).
HOME_ENV = "SANDLOOM_HOME"
MACHINE_ID_FILE = "machine-id"


def sandloom_home(environ: Mapping[str, str] | None = None) -> Path:
    """``$SANDLOOM_HOME``, else ``~/.sandloom``."""
    env = os.environ if environ is None else environ
    explicit = (env.get(HOME_ENV) or "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".sandloom"


def machine_id(environ: Mapping[str, str] | None = None) -> str:
    """This machine's id (12 hex), created on first use.

    Created with ``O_EXCL``, so two processes racing on a new machine agree on
    the one that won.
    """
    path = sandloom_home(environ) / MACHINE_ID_FILE
    with contextlib.suppress(OSError):
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = uuid.uuid4().hex[:12]
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip() or fresh
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(fresh + "\n")
    return fresh


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
