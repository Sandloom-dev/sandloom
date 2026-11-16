#!/usr/bin/env python3
"""PostToolUse hook: format the file Claude just edited, then report what a gate
would refuse in it, in the same turn.

Agents used to learn about a lint failure only when they ran a gate, several
turns after the edit (14-15 Nov: 1478 separate gate runs, 19% failing, a
median of five check -> edit cycles per agent). Per edited file:

* ``.py``: ``ruff format``, then ``ruff check --no-fix``; only findings the
  committed version of the file did not have are reported (a file with old
  findings stays quiet). F401 / F841 are skipped: an import or a variable
  added a moment before its first use is normal mid-edit.
* ``.md``: the prose rules of ``tools/docs/docs_prose_lint.py`` on the page,
  again only hits its committed version did not have (the page's code blocks
  stay with the docs gate: checking them imports the framework).
* ``.json``: the file must parse.

Everything runs with the tools of the checkout that holds the file (worktrees
included) and only inside this project. Findings go to stderr with exit 2,
which Claude Code shows to the agent; the edit itself is never undone, and
any failure of the hook exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from typing import Any

SKIP = {"F401", "F841"}
MAX_LINES = 15  # the report is re-sent on every later turn: keep it short


def _run(
    cmd: list[str], cwd: str | None, stdin: str | None = None, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed tools (git, ruff); the path is an argument
        cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout
    )


def _ruff(top: str) -> list[str]:
    exe = os.path.join(top, ".venv", "bin", "ruff")
    return [exe] if os.path.exists(exe) else ["uv", "run", "--no-sync", "ruff"]


def _findings(
    top: str, ruff: list[str], args: list[str], stdin: str | None = None
) -> list[dict[str, Any]]:
    out = _run(
        [*ruff, "check", "--no-fix", "--output-format", "json", *args], top, stdin
    )
    try:
        return [f for f in json.loads(out.stdout or "[]") if f.get("code") not in SKIP]
    except ValueError:
        return []


def check_py(path: str, rel: str, top: str) -> list[str]:
    ruff = _ruff(top)
    _run([*ruff, "format", "--quiet", "--force-exclude", path], top)
    now = _findings(top, ruff, ["--force-exclude", path])
    if not now:
        return []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    old = _run(["git", "show", f"HEAD:{rel}"], top)
    before: Counter[tuple[str, str, str]] = Counter()
    if old.returncode == 0:
        found = _findings(top, ruff, ["--stdin-filename", rel, "-"], old.stdout)
        before = Counter(_key(f, old.stdout) for f in found)
    seen: Counter[tuple[str, str, str]] = Counter()
    new: list[dict[str, Any]] = []
    for f in now:  # matched by code, message and line text, not by line number
        k = _key(f, text)
        seen[k] += 1
        if seen[k] > before[k]:
            new.append(f)
    return [f"{rel}:{f['location']['row']}: {f['code']} {f['message']}" for f in new]


def _key(finding: dict[str, Any], text: str) -> tuple[str, str, str]:
    lines = text.splitlines()
    row = finding["location"]["row"]
    line = lines[row - 1].strip() if 0 < row <= len(lines) else ""
    return finding["code"], finding["message"], line


# Runs inside the checkout's tools/docs: the hits of the page now, minus the
# hits its committed version already had (same rule, message and line text),
# so an edit to a page with old hits reports only what the edit added.
_MD_DIFF = r"""
import json, subprocess, sys
from collections import Counter
from pathlib import Path
import docs_prose_lint as L
rel = sys.argv[1]
event = json.dumps({"tool_input": {"file_path": sys.argv[2]}})
if L.hook_target(event, Path.cwd()) is None:
    sys.exit(0)
def keyed(text):
    if text is None:
        return []
    lines = text.splitlines()
    def line(n):
        return lines[n - 1].strip() if 0 < n <= len(lines) else ""
    return [((h.rule, h.message, line(h.line)), h) for h in L.lint_text(rel, text)]
now = keyed(open(sys.argv[2], encoding="utf-8").read())
old = subprocess.run(["git", "show", "HEAD:" + rel], capture_output=True, text=True)
before = Counter(k for k, _ in keyed(old.stdout if old.returncode == 0 else None))
seen = Counter()
for k, h in now:
    seen[k] += 1
    if seen[k] > before[k]:
        print(h.render())
"""


def check_md(path: str, rel: str, top: str) -> list[str]:
    tools = os.path.join(top, "tools", "docs")
    if not os.path.isfile(os.path.join(tools, "docs_prose_lint.py")):
        return []
    env = {**os.environ, "PYTHONPATH": tools}
    out = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", _MD_DIFF, rel, path],
        cwd=top,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout.strip().splitlines() if out.returncode == 0 else []


def check_json(path: str, rel: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    except ValueError as exc:
        return [f"{rel}: not valid JSON: {exc}"]
    return []


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (event.get("tool_input") or {}).get("file_path", "")
    if not path.endswith((".py", ".md", ".json")) or not os.path.isfile(path):
        return 0
    found = _run(
        ["git", "-C", os.path.dirname(path), "rev-parse", "--show-toplevel"],
        None,
        timeout=5,
    )
    top = found.stdout.strip()
    project = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR", ""))
    if not top or not project:
        return 0
    if os.path.commonpath([os.path.realpath(top), project]) != project:
        return 0
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    if path.endswith(".py"):
        report = check_py(path, rel, top)
        hint = "New ruff findings in the file you edited: fix them now."
    elif path.endswith(".md"):
        report = check_md(path, rel, top)
        hint = (
            "New docs prose-lint hits in the text you changed (tells.md, style.md):"
            " fix them, or waive the line with <!-- prose-ok: <rule> <reason> -->."
        )
    else:
        report = check_json(path, rel)
        hint = "Fix the JSON before anything reads it."
    if not report:
        return 0
    if len(report) > MAX_LINES:
        report = [*report[:MAX_LINES], f"… and {len(report) - MAX_LINES} more"]
    print("\n".join([*report, hint]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a broken hook must never block an edit
        sys.exit(0)
