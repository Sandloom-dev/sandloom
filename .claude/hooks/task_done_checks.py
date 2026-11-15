#!/usr/bin/env python3
"""PreToolUse hook on tm's ``task_done``: run the checks at the handover.

The tm-fleet procedure (``.agents/skills/tm-fleet/SKILL.md``, section 8):
where the repository runs its checks at ``task_done``, the first handover is
refused once with a ``{"kind": "checks", ...}`` deliverable in the reason; the
agent calls ``task_done`` again with that deliverable appended, unchanged, and
the handover goes through carrying a record that was run, not retold. A red
result does not block it: it is recorded for whoever reviews.

Only a ``sandloom-implementer`` subagent's handover is checked (the hook input
names the subagent type). The run is the static gate of the agent's checkout:
``pre-commit run --from-ref origin/main --to-ref HEAD`` with the dummy settings
CI gives the same job, skipping the architecture tests (they need the agent's
own test servers, which a hook does not know) and the two hooks that rewrite
files (a dirty tree would make tm refuse the handover). Any failure of the hook
itself lets the call through.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

AGENT = "sandloom-implementer"
SKIP = "architecture-tests,end-of-file-fixer,trailing-whitespace"
CI_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://ci:ci@127.0.0.1:5433/ci",
    "CACHE_URL": "redis://127.0.0.1:6380/0",
    "BROKER_URL": "redis://127.0.0.1:6380/1",
    "JWT_SECRET": "ci-architecture-tests-not-a-real-secret",
}
_RESULT = re.compile(r"^(.+?)\.{3,}(?:\(.*\))?(Passed|Failed|Skipped)\s*$")


def _git(args: list[str], cwd: str) -> str:
    argv = ["git", *args]
    done = subprocess.run(  # noqa: S603 - fixed git argv
        argv, cwd=cwd, capture_output=True, text=True, timeout=10, check=False
    )
    return done.stdout.strip()


def _deliverables(tool_input: dict) -> list[dict]:
    raw = tool_input.get("deliverables") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [d for d in raw if isinstance(d, dict)]


def already_checked(tool_input: dict, head: str) -> bool:
    """A ``checks`` deliverable for this very commit is already attached."""
    for item in _deliverables(tool_input):
        if item.get("kind") != "checks":
            continue
        try:
            record = json.loads(item.get("content") or "{}")
        except ValueError:
            continue
        if record.get("commit") == head:
            return True
    return False


def results(output: str) -> dict[str, str]:
    """``{hook name: Passed|Failed|Skipped}`` from pre-commit's output."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        m = _RESULT.match(line.strip())
        if m:
            found[m.group(1).strip()] = m.group(2)
    return found


def run_checks(top: str) -> tuple[dict[str, str], str]:
    env = {**os.environ, **CI_ENV, "SKIP": SKIP}
    argv = ["uv", "run", "--locked", "pre-commit", "run"]
    argv += ["--from-ref", "origin/main", "--to-ref", "HEAD"]
    done = subprocess.run(  # noqa: S603 - fixed argv in the agent's checkout
        argv,
        cwd=top,
        env=env,
        capture_output=True,
        text=True,
        timeout=540,
        check=False,
    )
    output = done.stdout + done.stderr
    failed = [
        ln for ln in output.splitlines() if ln.strip() and not _RESULT.match(ln.strip())
    ]
    return results(output), "\n".join(failed[-25:])


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("agent_type") != AGENT:
        return 0
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    head = _git(["rev-parse", "HEAD"], cwd)
    if not top or not head or already_checked(tool_input, head):
        return 0
    outcome, tail = run_checks(top)
    if not outcome:
        return 0
    record = {
        "commit": head,
        "ran": "pre-commit run --from-ref origin/main --to-ref HEAD",
        "skipped": SKIP.split(","),
        "results": outcome,
        "failed": sorted(k for k, v in outcome.items() if v == "Failed"),
    }
    if record["failed"]:
        record["tail"] = tail
    deliverable = {"kind": "checks", "content": json.dumps(record, sort_keys=True)}
    reason = (
        "Checks ran at the handover (tm-fleet: checks at task_done). Call task_done "
        "again with the same arguments and this deliverable appended, unchanged, "
        "editing nothing in between; a red result does not block the handover:\n"
        + json.dumps(deliverable)
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a broken hook must never block a handover
        sys.exit(0)
