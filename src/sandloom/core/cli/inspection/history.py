"""Read a committed inspection index as data, without checking out code."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import sandloom.core.cli.tool_process as _tool_process
from sandloom.core.cli.inspection.files import MAX_FILE, checked_content
from sandloom.core.cli.inspection.model import INDEX, Doc, InspectionError
from sandloom.core.cli.inspection.read import parse


def _git(root: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise InspectionError(
            "git_unavailable", "Git is not installed; use --before PATH"
        )
    try:
        result = _tool_process.run_tool(
            [executable, "--no-optional-locks", *args],
            cwd=root,
            capture=True,
            timeout=10,
            encoding="utf-8",
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError(
            "git_unavailable", "Cannot read local Git history; use --before PATH"
        ) from exc
    if result.returncode:
        raise InspectionError(
            "git_baseline_unavailable",
            "No local revision/index found; commit the generated inspection "
            "index on that revision or use --before PATH",
        )
    return result.stdout


def read(root: Path, revision: str) -> tuple[Doc, str, str]:
    """Resolve once to an immutable commit and read this project's bounded blob."""
    commit = _git(
        root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"
    ).strip()
    prefix = _git(root, "rev-parse", "--show-prefix").rstrip("\n")
    locator = prefix + INDEX
    object_id = f"{commit}:{locator}"
    size = int(_git(root, "cat-file", "-s", object_id))
    if size > MAX_FILE:
        raise InspectionError(
            "file_too_large", f"Historical inspection index exceeds {MAX_FILE} bytes"
        )
    content = _git(root, "cat-file", "blob", object_id).encode("utf-8")
    return parse(checked_content(content, locator)), f"{revision}:{locator}", commit
