"""Wait on a pull request's CI in one call, and merge it on green if asked.

    python3 tools/ci/pr_wait.py <PR> [--merge] [--head SHA] [--required]
                                     [--interval S] [--timeout S]

Meant for a fleet orchestrator to run in the background (Bash
``run_in_background``): the wait costs no model turns, and the one turn when it
exits reads at most six lines. Replaces the check / wait / recheck the head and
mergeability / merge round of ``gh pr checks`` and ``gh pr merge`` calls.

It polls until no check is pending and every check branch protection requires
is registered and finished (right after a push GitHub lists only some checks:
a verdict then would be on a half-registered CI, and the merge refused by
branch policy; a repository with no required check never settles and times
out), then prints the verdict, the head, the
failing checks with the link of the first, and GitHub's merge state. With
``--merge`` it merges (a merge commit, ``--match-head-commit`` the head it
judged) only when every check passed and GitHub reports the PR mergeable.
``--head`` fails fast when the PR's head is not the commit you mean to merge.
``--required`` judges only the checks branch protection requires.

Exit codes: 0 green (merged with ``--merge``), 1 a check failed, was cancelled
or sits in a bucket this tool does not know, 2 not mergeable / head moved / PR
closed / merge refused / a ``gh`` call that kept failing (printed as ``ERROR``,
also at the deadline), 3 timed out (checks pending or a required check not
registered).
Standard library and ``gh`` only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

GREEN, RED, NOT_MERGEABLE, TIMEOUT = 0, 1, 2, 3
# An allow-list: a bucket gh adds later (or a row without one) counts as failed,
# never as green. ``pending`` is judged separately and is not a verdict yet.
NOT_FAILED_BUCKETS = frozenset({"pass", "skipping", "pending"})
# Consecutive ``gh`` failures one poll tolerates (a network blip, a rate limit)
# before it raises; the third in a row is a broken gh, not a blip.
TOLERATED_GH_FAILURES = 2
# ``gh pr checks`` exit codes with JSON on stdout: 0 all passed, 8 some pending.
PENDING_OR_OK = (0, 8)
# What gh prints to stderr (exit 1, stdout empty) before any check registered.
NO_CHECKS_MESSAGES = ("no checks reported", "no required checks reported")

Runner = Callable[[Sequence[str]], "Result"]


@dataclass
class Result:
    code: int
    out: str
    err: str = ""


def run_gh(args: Sequence[str]) -> Result:
    proc = subprocess.run(  # noqa: S603 - fixed gh CLI, arguments built here
        ["gh", *args],  # noqa: S607 - gh is resolved from PATH like every fleet tool
        capture_output=True,
        text=True,
        check=False,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class State:
    head: str
    pr_state: str
    merge_state: str
    mergeable: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    # The checks branch protection requires, as GitHub reports them now. Right
    # after a push only some checks are registered: a required one missing from
    # this list means the CI is not fully registered yet, whatever else passed.
    # The settled rule is "some required check is present", which equals "every
    # required check" only while branch protection requires one check
    # ("CI required"): gh cannot list a required check that is not registered.
    # test_pr_wait.py pins that assumption: a second required check would need
    # the names from branch protection.
    required: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pending(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c.get("bucket") == "pending"]

    @property
    def registered(self) -> bool:
        """Every required check is present and finished, and no check is pending."""
        return (
            bool(self.checks)
            and bool(self.required)
            and not self.pending
            and not any(c.get("bucket") == "pending" for c in self.required)
        )

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c.get("bucket") not in NOT_FAILED_BUCKETS]


def read_state(pr: str, required: bool, gh: Runner) -> State:
    view = gh(
        ["pr", "view", pr, "--json", "headRefOid,state,mergeStateStatus,mergeable"]
    )
    if view.code != 0:
        raise RuntimeError(f"gh pr view {pr}: {view.err.strip() or view.out.strip()}")
    v = json.loads(view.out)
    # Three ``gh`` calls a poll (two with --required, which one call serves):
    # ``gh pr view --json statusCheckRollup`` carries neither the bucket nor the
    # required flag, so the view cannot replace the two ``gh pr checks`` calls.
    required_rows = _checks(pr, True, gh)
    # With --required the judged set is the required one: one call serves both.
    rows = required_rows if required else _checks(pr, False, gh)
    return State(
        v["headRefOid"],
        v["state"],
        v["mergeStateStatus"],
        v["mergeable"],
        rows,
        required_rows,
    )


def _checks(pr: str, required: bool, gh: Runner) -> list[dict[str, Any]]:
    args = ["pr", "checks", pr, "--json", "name,bucket,link,workflow"]
    if required:
        args.append("--required")
    # gh pr checks exits 8 while checks are pending and 1 when one failed;
    # the JSON on stdout is what counts. No checks reported yet: stdout empty
    # and gh says so on stderr (exit 1). Any other empty-stdout failure (auth,
    # network, rate limit) is a broken gh, not a CI that has not registered.
    checks = gh(args)
    if (
        checks.code not in PENDING_OR_OK
        and not checks.out.strip()
        and not any(m in checks.err for m in NO_CHECKS_MESSAGES)
    ):
        raise RuntimeError(
            f"gh pr checks {pr}: {checks.err.strip() or f'exit {checks.code}'}"
        )
    try:
        rows: list[dict[str, Any]] = (
            json.loads(checks.out) if checks.out.strip() else []
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh pr checks {pr}: {checks.err.strip() or checks.out.strip()}"
        ) from exc
    return rows


def summary(verdict: str, s: State) -> list[str]:
    lines = [
        f"{verdict} head={s.head[:8]} checks={len(s.checks)}"
        f" merge_state={s.merge_state} mergeable={s.mergeable}"
    ]
    if s.failed:
        names = ", ".join(
            f"{c.get('workflow', '')}/{c['name']}".lstrip("/") for c in s.failed
        )
        lines.append(f"failed ({len(s.failed)}): {names}"[:300])
        lines.append(f"first failure: {s.failed[0].get('link', '')}")
    if s.pending:
        lines.append(
            f"pending ({len(s.pending)}): "
            + ", ".join(c["name"] for c in s.pending)[:280]
        )
    if not s.required:
        lines.append("no required check registered yet (branch protection set)")
    return lines


def poll(
    pr: str,
    *,
    merge: bool,
    head: str | None,
    required: bool,
    interval: float,
    timeout: float,
    gh: Runner,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> State | tuple[int, list[str]]:
    """The settled state, or the exit code and lines when there is none."""
    deadline = clock() + timeout
    failures = 0
    while True:
        try:
            s = read_state(pr, required, gh)
        except RuntimeError:
            failures += 1
            if failures > TOLERATED_GH_FAILURES or clock() >= deadline:
                raise
            sleep(interval)
            continue
        failures = 0
        if s.pr_state == "MERGED":
            return GREEN, [f"already merged head={s.head[:8]}"]
        if s.pr_state != "OPEN":
            return NOT_MERGEABLE, [f"PR is {s.pr_state} head={s.head[:8]}"]
        if head and not s.head.startswith(head):
            return NOT_MERGEABLE, [
                f"head moved: PR head {s.head[:8]}, expected {head[:8]}"
            ]
        settled = s.registered
        # GitHub computes mergeability lazily: UNKNOWN right after a push.
        if settled and not (merge and s.mergeable == "UNKNOWN" and not s.failed):
            return s
        if clock() >= deadline:
            return TIMEOUT, summary("TIMEOUT", s)
        sleep(interval)


def wait(
    pr: str,
    *,
    merge: bool = False,
    head: str | None = None,
    required: bool = False,
    interval: float = 30,
    timeout: float = 3600,
    gh: Runner = run_gh,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, list[str]]:
    s = poll(
        pr,
        merge=merge,
        head=head,
        required=required,
        interval=interval,
        timeout=timeout,
        gh=gh,
        sleep=sleep,
        clock=clock,
    )
    if not isinstance(s, State):
        return s
    if s.failed:
        return RED, summary("RED", s)
    if not merge:
        return GREEN, summary("GREEN", s)
    if s.mergeable != "MERGEABLE":
        return NOT_MERGEABLE, summary("NOT MERGEABLE", s)
    res = gh(["pr", "merge", pr, "--merge", "--match-head-commit", s.head])
    if res.code != 0:
        why = (res.err.strip() or res.out.strip())[:300]
        return NOT_MERGEABLE, [*summary("MERGE REFUSED", s), why]
    return GREEN, summary("MERGED", s)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    p.add_argument("pr", help="PR number, URL or branch")
    p.add_argument(
        "--merge",
        action="store_true",
        help="merge (merge commit) when green and mergeable",
    )
    p.add_argument(
        "--head", help="the head SHA (or prefix) you mean; fail fast if the PR moved"
    )
    p.add_argument(
        "--required", action="store_true", help="judge only the required checks"
    )
    p.add_argument(
        "--interval", type=float, default=30, help="seconds between polls (default 30)"
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=3600,
        help="give up after this many seconds (default 3600)",
    )
    a = p.parse_args(argv)
    try:
        code, lines = wait(
            a.pr,
            merge=a.merge,
            head=a.head,
            required=a.required,
            interval=a.interval,
            timeout=a.timeout,
        )
    except (RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}"[:400])
        return NOT_MERGEABLE
    print("\n".join(lines[:6]))
    return code


if __name__ == "__main__":
    sys.exit(main())
