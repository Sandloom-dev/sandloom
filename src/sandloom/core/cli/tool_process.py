"""Run one of a project's own dev tools in a child process.

Shared by ``sandloom test`` (its pytest / ruff / mypy / lint-imports steps) and
``sandloom new`` (the generated-files writer). Standard library only: importing
it builds no settings, so static commands stay environment-free.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_tool(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in ``cwd`` (``capture``: stdout + stderr in ``.stdout``;
    ``stdin``: the text fed to the tool)."""
    return subprocess.run(  # noqa: S603 - fixed argv of the project's own dev tools (pytest, ruff, mypy, lint-imports, the generated-files writer), never user input
        argv,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        input=stdin,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


__all__ = ["run_tool"]
