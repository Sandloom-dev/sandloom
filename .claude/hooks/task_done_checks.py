#!/usr/bin/env python3
"""PreToolUse hook on tm's ``task_done``: run the checks at the handover.

The tm-fleet procedure (``.agents/skills/tm-fleet/SKILL.md``, section 8):
where the repository runs its checks at ``task_done``, the first handover is
refused once with a ``{"kind": "checks", ...}`` deliverable in the reason; the
agent calls ``task_done`` again with that deliverable appended, unchanged, and
the handover goes through carrying a record that was run, not retold. A red
result does not block it: it is recorded for whoever reviews.

Only a ``sandloom-implementer`` subagent's handover is checked (the hook input
names the subagent type). The agent's checkout is the worktree whose HEAD is the
commit its ``git_commit`` deliverable names (the hook's cwd can be the main
checkout); none at that commit gives a ``not_run`` record, never a run of another
tree. The run is the static gate of that checkout:
``pre-commit run --from-ref origin/main --to-ref HEAD`` with the dummy settings
CI gives the same job, skipping the architecture tests (they need the agent's
own test servers, which a hook does not know) and the two hooks that rewrite
files (a dirty tree would make tm refuse the handover). Any failure of the hook
itself lets the call through.

Only the record this hook emitted passes the second call: its digest is kept in
``~/.cache/sandloom-checks`` (outside every checkout), so an agent cannot attach
a self-written all-green record. If that directory cannot be written, the hook
lets the handover through without a record (the catch-all below) rather than
refusing a second call it could never accept. Credentials (URL userinfo and
secret-looking query values) are masked in everything stored in tm.

Trust: the run uses the agent's own checkout, its ``.pre-commit-config.yaml`` and
``uv.lock`` included, with this machine's environment, exactly as the agent's own
pre-commit run would. The record is evidence of what that tree's gates said, not a
boundary against a hostile agent: CI on GitHub stays the gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

AGENT = "sandloom-implementer"
SKIP = "architecture-tests,end-of-file-fixer,trailing-whitespace"
CI_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://ci:ci@127.0.0.1:5433/ci",
    "CACHE_URL": "redis://127.0.0.1:6380/0",
    "BROKER_URL": "redis://127.0.0.1:6380/1",
    "JWT_SECRET": "ci-architecture-tests-not-a-real-secret",
}
_RESULT = re.compile(r"^(.+?)\.{3,}(?:\(.*\))?(Passed|Failed|Skipped)\s*$")
# user:pass@ and the password-only :pass@ form (redis://:secret@host).
# Any userinfo in a URL: user:pass@, :pass@ and a bare token@ (https://<token>@host).
_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://[^/\s@]+@")
# Secret-looking query values: ?token=..., &password=..., api_key=...
_SECRET_QUERY = re.compile(
    r"([?&;](?:[A-Za-z0-9_.-]*?(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|auth))=)[^&#\s]+",
    re.IGNORECASE,
)
_SHA = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
#: Digests of the records this hook emitted, outside every checkout.
ISSUED = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "sandloom-checks"
)


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


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def remember(content: str) -> None:
    """Note the record this hook emitted, outside any checkout an agent edits."""
    ISSUED.mkdir(parents=True, exist_ok=True)
    (ISSUED / _digest(content)).write_text(content, encoding="utf-8")


def already_checked(tool_input: dict, head: str) -> bool:
    """The ``checks`` deliverable this hook emitted for this very commit is
    attached. A record the agent wrote itself (same shape, all green) is not
    one: only a content whose digest the hook stored when it refused passes."""
    for item in _deliverables(tool_input):
        if item.get("kind") != "checks":
            continue
        content = item.get("content")
        if not isinstance(content, str) or not (ISSUED / _digest(content)).is_file():
            continue
        try:
            record = json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and record.get("commit") == head:
            return True
    return False


def redact(text: str) -> str:
    """Credentials out of text stored in tm: URL userinfo (``user:pass@``,
    ``:pass@``, ``token@``) and secret-looking query values."""
    return _SECRET_QUERY.sub(r"\1***", _CREDENTIALS.sub(r"\1://***@", text))


def results(output: str) -> dict[str, str]:
    """``{hook name: Passed|Failed|Skipped}`` from pre-commit's output."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        m = _RESULT.match(line.strip())
        if m:
            found[m.group(1).strip()] = m.group(2)
    return found


def run_checks(top: str) -> tuple[dict[str, str], str, int]:
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
    return results(output), "\n".join(failed[-25:]), done.returncode


def handed_over(tool_input: dict, cwd: str) -> tuple[bool, str | None]:
    """``(named, sha)``: whether a ``git_commit`` deliverable is attached, and
    the full SHA of the first commit of this clone it names. An abbreviated
    SHA is resolved (``rev-parse <sha>^{commit}``); a full one that does not
    resolve is kept, so the record names what was handed over."""
    named = False
    for item in _deliverables(tool_input):
        if item.get("kind") != "git_commit":
            continue
        named = True
        text = item.get("content") or item.get("reference")
        for token in _SHA.findall(text) if isinstance(text, str) else []:
            full = _git(
                ["rev-parse", "--verify", "--quiet", f"{token}^{{commit}}"], cwd
            )
            if full:
                return True, full.lower()
            if len(token) == 40:
                return True, token.lower()
    return named, None


def checkout_at(sha: str, cwd: str) -> str | None:
    """The worktree of this clone whose HEAD is ``sha``: the agent's own, not
    the session's cwd (an agent that made its own worktree still reports the
    main checkout as its cwd). Of several at that commit (a stale named
    worktree beside the agent's), a clean one wins."""
    listing = _git(["worktree", "list", "--porcelain"], cwd)
    path = None
    at: list[str] = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("HEAD ") and line[len("HEAD ") :].lower() == sha and path:
            at.append(path)
    for tree in at:
        if not _git(["status", "--porcelain"], tree):
            return tree
    return at[0] if at else None


def locate(tool_input: dict, cwd: str) -> tuple[str, str, str]:
    """``(checkout, head, why_not)``: the tree to check, its commit, and why
    it cannot be checked (then the record says so instead of checking another
    tree). Only a handover with no ``git_commit`` deliverable falls back to
    the cwd's checkout."""
    named, sha = handed_over(tool_input, cwd)
    if sha:
        top = checkout_at(sha, cwd)
        if top:
            return top, sha, ""
        return "", sha, f"no worktree of this clone is at the handed-over {sha[:12]}"
    if named:
        head = _git(["rev-parse", "HEAD"], cwd)
        return "", head, "the git_commit deliverable names no commit of this clone"
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    return top, _git(["rev-parse", "HEAD"], top or cwd), ""


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("agent_type") != AGENT:
        return 0
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    top, head, unlocated = locate(tool_input, cwd)
    if not head or already_checked(tool_input, head):
        return 0
    if not top and not unlocated:
        return 0
    exit_status: int | None = None
    outcome: dict[str, str] = {}
    tail = ""
    if unlocated:
        # Checking another tree (the main checkout) would record its results
        # under this commit: say why there are none instead.
        not_run = unlocated
    else:
        try:
            outcome, tail, exit_status = run_checks(top)
            # Mask first, then cut: a cut could drop a URL's prefix, keep its password.
            shown = redact(tail)[-300:]
            not_run = "" if outcome else "pre-commit printed no hook result: " + shown
            if _git(["rev-parse", "HEAD"], top) != head:
                # The tree moved under the run: its results belong to no commit.
                outcome, not_run = {}, "HEAD moved while the checks ran"
        except (subprocess.TimeoutExpired, OSError) as exc:
            outcome, tail = {}, ""
            not_run = redact(f"{type(exc).__name__}: {exc}")[:300]
    record = {
        "commit": head,
        "ran": "pre-commit run --from-ref origin/main --to-ref HEAD",
        "skipped": SKIP.split(","),
        "results": outcome,
        "failed": sorted(k for k, v in outcome.items() if v == "Failed"),
        "exit_status": exit_status,
    }
    if not_run:
        # Recorded once, so the reviewer sees why there is no result; the
        # second call carries it through like any other record.
        record["not_run"] = redact(not_run)
    if record["failed"] or exit_status not in (0, None):
        # A nonzero exit with no failed hook is an errored or partial run:
        # keep its output, so it never reads as a clean pass.
        record["tail"] = redact(tail)
    deliverable = {"kind": "checks", "content": json.dumps(record, sort_keys=True)}
    remember(deliverable["content"])
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
