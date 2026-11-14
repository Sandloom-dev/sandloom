#!/usr/bin/env python3
"""PostToolUse hook: format a Python file right after Claude edits it.

Every gate here (CI, ``sandloom test``, pre-commit) only *checks*
``ruff format``; nothing formatted at the source, so a long line written by an
agent surfaced only at a gate and cost a fix-up round. This runs
``ruff format`` on the one edited file, with the checkout's own ruff (worktrees
included), honouring the project's excludes. It never blocks an edit: any
failure exits 0. ``ruff check --fix`` is deliberately not run here (F401 would
delete an import the agent has added but not used yet).
"""

import json
import os
import subprocess
import sys

try:
    path = json.load(sys.stdin).get("tool_input", {}).get("file_path", "")
except Exception:
    sys.exit(0)
if not path.endswith(".py") or not os.path.isfile(path):
    sys.exit(0)
try:
    folder = os.path.dirname(path)
    top = (
        subprocess.run(
            ["git", "-C", folder, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        or folder
    )
    ruff = os.path.join(top, ".venv", "bin", "ruff")
    cmd = [ruff] if os.path.exists(ruff) else ["uv", "run", "--no-sync", "ruff"]
    subprocess.run(
        [*cmd, "format", "--quiet", "--force-exclude", path],
        cwd=top,
        capture_output=True,
        timeout=10,
    )
except Exception:
    pass
sys.exit(0)
