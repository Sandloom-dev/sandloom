"""The child processes of ``sandloom dev``: ``git``, ``docker``, ``lsof`` and
the app server. One place runs them: a fixed argv (never a shell), the
executable resolved on ``PATH`` first."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


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


__all__ = ["run", "spawn", "which"]
