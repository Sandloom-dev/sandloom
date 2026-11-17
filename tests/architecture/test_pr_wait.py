"""``tools/ci/pr_wait.py`` waits on a PR's checks and merges only on green.

A fleet orchestrator runs it in the background instead of polling ``gh pr
checks`` turn by turn. These tests drive it with a scripted fake ``gh``: the
verdict per check state, the exit codes, the head guard, and that ``--merge``
merges exactly the head it judged and never a red, moved or unmergeable PR.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "ci" / "pr_wait.py"
HEAD = "784e2c231a37e6361ecfb1385c702472be0b7155"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr_wait_under_test", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pr_wait = _load()


def check(name: str, bucket: str) -> dict[str, str]:
    return {
        "name": name,
        "bucket": bucket,
        "workflow": "Monorepo CI",
        "link": f"https://example.invalid/{name}",
    }


class FakeGh:
    """Answers ``gh`` from a list of polls; records every call."""

    def __init__(
        self,
        polls: list[list[dict[str, str]]],
        *,
        head: str = HEAD,
        state: str = "OPEN",
        mergeable: str | list[str] = "MERGEABLE",
        merge_code: int = 0,
        required: set[str] | None = None,
    ) -> None:
        self.polls = polls
        # Names branch protection requires; None: every check the poll lists.
        self.required = required
        self.head = head
        self.state = state
        self.mergeable = mergeable
        self.merge_code = merge_code
        self.calls: list[list[str]] = []
        self._view = 0

    def __call__(self, args: Sequence[str]) -> object:
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            m = self.mergeable
            value = m[min(self._view, len(m) - 1)] if isinstance(m, list) else m
            self._view += 1
            body = {
                "headRefOid": self.head,
                "state": self.state,
                "mergeStateStatus": "CLEAN",
                "mergeable": value,
            }
            return pr_wait.Result(0, json.dumps(body))
        if args[:2] == ["pr", "checks"]:
            # A poll is one view, then its checks calls (required, full list).
            rows = self.polls[min(self._view - 1, len(self.polls) - 1)]
            if "--required" in args and self.required is not None:
                rows = [r for r in rows if r["name"] in self.required]
            return pr_wait.Result(
                8 if any(r["bucket"] == "pending" for r in rows) else 0,
                json.dumps(rows),
            )
        if args[:2] == ["pr", "merge"]:
            return pr_wait.Result(
                self.merge_code, "", "" if self.merge_code == 0 else "refused"
            )
        raise AssertionError(args)

    @property
    def merges(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


def run(gh: FakeGh, **kw: object) -> tuple[int, list[str], list[float]]:
    slept: list[float] = []
    t = [0.0]

    def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    code, lines = pr_wait.wait(
        "276", gh=gh, sleep=sleep, clock=lambda: t[0], interval=30, **kw
    )
    return code, lines, slept


def test_waits_through_pending_then_reports_green_without_merging() -> None:
    gh = FakeGh(
        [
            [check("a", "pending")],
            [check("a", "pending")],
            [check("a", "pass"), check("b", "skipping")],
        ]
    )
    code, lines, slept = run(gh)
    assert code == pr_wait.GREEN
    assert lines[0].startswith("GREEN head=784e2c23")
    assert slept == [30, 30]
    assert gh.merges == []


def test_no_checks_reported_yet_counts_as_pending() -> None:
    gh = FakeGh([[], [check("a", "pass")]])
    code, _, slept = run(gh)
    assert code == pr_wait.GREEN and slept == [30]


def test_red_names_failures_and_never_merges() -> None:
    gh = FakeGh([[check("a", "pass"), check("lint", "fail"), check("x", "cancel")]])
    code, lines, _ = run(gh, merge=True)
    assert code == pr_wait.RED
    assert "Monorepo CI/lint" in lines[1] and "(2)" in lines[1]
    assert lines[2] == "first failure: https://example.invalid/lint"
    assert gh.merges == []


def test_merge_on_green_matches_the_judged_head() -> None:
    gh = FakeGh([[check("a", "pass")]])
    code, lines, _ = run(gh, merge=True)
    assert code == pr_wait.GREEN and lines[0].startswith("MERGED")
    assert gh.merges == [["pr", "merge", "276", "--merge", "--match-head-commit", HEAD]]


def test_merge_waits_for_github_to_compute_mergeability() -> None:
    gh = FakeGh([[check("a", "pass")]], mergeable=["UNKNOWN", "MERGEABLE"])
    code, _, slept = run(gh, merge=True)
    assert code == pr_wait.GREEN and slept == [30] and len(gh.merges) == 1


def test_conflicting_pr_is_not_merged() -> None:
    gh = FakeGh([[check("a", "pass")]], mergeable="CONFLICTING")
    code, lines, _ = run(gh, merge=True)
    assert code == pr_wait.NOT_MERGEABLE and lines[0].startswith("NOT MERGEABLE")
    assert gh.merges == []


def test_refused_merge_reports_gh_error() -> None:
    gh = FakeGh([[check("a", "pass")]], merge_code=1)
    code, lines, _ = run(gh, merge=True)
    assert code == pr_wait.NOT_MERGEABLE and lines[-1] == "refused"


def test_head_guard_fails_fast() -> None:
    gh = FakeGh([[check("a", "pending")]], head="deadbeef" + "0" * 32)
    code, lines, slept = run(gh, merge=True, head=HEAD[:8])
    assert code == pr_wait.NOT_MERGEABLE and lines[0].startswith("head moved")
    assert slept == [] and gh.merges == []


@pytest.mark.parametrize(("state", "expected"), [("MERGED", 0), ("CLOSED", 2)])
def test_closed_or_merged_pr_ends_at_once(state: str, expected: int) -> None:
    code, _, slept = run(FakeGh([[check("a", "pending")]], state=state))
    assert code == expected and slept == []


def test_timeout_lists_what_is_still_pending() -> None:
    gh = FakeGh([[check("slow", "pending"), check("a", "pass")]])
    code, lines, _ = run(gh, timeout=100)
    assert code == pr_wait.TIMEOUT
    assert lines[0].startswith("TIMEOUT") and lines[-1] == "pending (1): slow"


def test_half_registered_ci_is_not_judged_until_the_required_check_appears() -> None:
    # Right after a push: three finished non-required checks, the required one
    # not registered yet. The merge must wait for it, not be refused by policy.
    early = [check("a", "pass"), check("b", "pass"), check("c", "skipping")]
    gh = FakeGh(
        [early, early, [*early, check("gate", "pass")]],
        required={"gate"},
    )
    code, lines, slept = run(gh, merge=True)
    assert code == pr_wait.GREEN and lines[0].startswith("MERGED")
    assert slept == [30, 30]
    assert len(gh.merges) == 1


def test_required_set_is_read_even_without_the_required_flag() -> None:
    gh = FakeGh([[check("a", "pass")]])
    run(gh)
    required_calls = [c for c in gh.calls if c[:2] == ["pr", "checks"]]
    assert any("--required" in c for c in required_calls)
    assert any("--required" not in c for c in required_calls)


def test_required_check_never_registering_times_out_without_merging() -> None:
    gh = FakeGh([[check("a", "pass")]], required={"gate"})
    code, lines, _ = run(gh, merge=True, timeout=100)
    assert code == pr_wait.TIMEOUT
    assert lines[-1].startswith("no required check registered")
    assert gh.merges == []


def test_required_check_pending_blocks_the_verdict() -> None:
    gh = FakeGh(
        [[check("a", "pass"), check("gate", "pending")]],
        required={"gate"},
    )
    code, lines, _ = run(gh, merge=True, timeout=100)
    assert code == pr_wait.TIMEOUT and lines[-1] == "pending (1): gate"
    assert gh.merges == []


def test_failed_required_check_is_red_once_registered() -> None:
    gh = FakeGh(
        [[check("a", "pass")], [check("a", "pass"), check("gate", "fail")]],
        required={"gate"},
    )
    code, lines, slept = run(gh, merge=True)
    assert code == pr_wait.RED and "gate" in lines[1]
    assert slept == [30] and gh.merges == []


def _checks_failing(code: int, err: str) -> FakeGh:
    """``gh`` whose ``pr checks`` calls fail with empty stdout, as gh does."""

    class Failing(FakeGh):
        def __call__(self, args: Sequence[str]) -> object:
            if args[:2] == ["pr", "checks"]:
                self.calls.append(list(args))
                return pr_wait.Result(code, "", err)
            return super().__call__(args)

    return Failing([[check("a", "pass")]])


@pytest.mark.parametrize("err", ["HTTP 401: Bad credentials", "", "connection reset"])
def test_broken_gh_with_empty_checks_raises_instead_of_polling(err: str) -> None:
    gh = _checks_failing(1, err)
    with pytest.raises(RuntimeError, match="gh pr checks 276"):
        run(gh, timeout=100)


@pytest.mark.parametrize(
    "err",
    [
        "no checks reported on the 'claude/x' branch",
        "no required checks reported on the 'claude/x' branch",
    ],
)
def test_no_checks_reported_message_keeps_polling(err: str) -> None:
    gh = _checks_failing(1, err)
    code, lines, slept = run(gh, timeout=100)
    assert code == pr_wait.TIMEOUT and slept
    assert lines[-1].startswith("no required check registered")


def test_broken_gh_exits_error_code_2_from_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(cmd: Sequence[str], **_: object) -> object:
        import subprocess

        if cmd[2] == "view":
            body = {
                "headRefOid": HEAD,
                "state": "OPEN",
                "mergeStateStatus": "CLEAN",
                "mergeable": "MERGEABLE",
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(cmd, 1, "", "HTTP 401: Bad credentials")

    monkeypatch.setattr(pr_wait.subprocess, "run", fake_run)
    # Interval 0: the tolerated failures are retried at once, the third raises.
    argv = ["276", "--timeout", "100", "--interval", "0"]
    assert pr_wait.main(argv) == pr_wait.NOT_MERGEABLE
    assert capsys.readouterr().out.startswith("ERROR gh pr checks 276: HTTP 401")


def test_required_flag_reaches_gh() -> None:
    gh = FakeGh([[check("a", "pass")]])
    run(gh, required=True)
    assert [
        "pr",
        "checks",
        "276",
        "--json",
        "name,bucket,link,workflow",
        "--required",
    ] in gh.calls


@pytest.mark.parametrize("bucket", ["fail", "cancel", "stale", "unknown", ""])
def test_a_bucket_outside_the_allow_list_is_red(bucket: str) -> None:
    gh = FakeGh([[check("a", "pass"), check("b", bucket)]])
    code, lines, _ = run(gh, merge=True)
    assert code == pr_wait.RED and "b" in lines[1]
    assert gh.merges == []


@pytest.mark.parametrize("bucket", ["pass", "skipping"])
def test_pass_and_skipping_are_not_red(bucket: str) -> None:
    code, _, _ = run(FakeGh([[check("a", bucket)]]))
    assert code == pr_wait.GREEN


class Flaky(FakeGh):
    """``pr view`` fails ``failures`` times in a row, then answers."""

    def __init__(self, failures: int, polls: list[list[dict[str, str]]]) -> None:
        super().__init__(polls)
        self.failures = failures

    def __call__(self, args: Sequence[str]) -> object:
        if args[:2] == ["pr", "view"] and self.failures:
            self.failures -= 1
            self.calls.append(list(args))
            return pr_wait.Result(1, "", "connection reset")
        return super().__call__(args)


def test_two_consecutive_gh_failures_are_tolerated() -> None:
    gh = Flaky(pr_wait.TOLERATED_GH_FAILURES, [[check("a", "pass")]])
    code, _, slept = run(gh)
    assert code == pr_wait.GREEN
    assert slept == [30] * pr_wait.TOLERATED_GH_FAILURES


def test_the_third_consecutive_gh_failure_raises() -> None:
    gh = Flaky(pr_wait.TOLERATED_GH_FAILURES + 1, [[check("a", "pass")]])
    with pytest.raises(RuntimeError, match="gh pr view 276: connection reset"):
        run(gh)


def test_a_success_between_failures_resets_the_count() -> None:
    class Alternating(FakeGh):
        def __init__(self) -> None:
            super().__init__([[check("a", "pending")]] * 2 + [[check("a", "pass")]])
            self.views = 0

        def __call__(self, args: Sequence[str]) -> object:
            if args[:2] == ["pr", "view"]:
                self.views += 1
                if self.views % 3 != 0:
                    self.calls.append(list(args))
                    return pr_wait.Result(1, "", "connection reset")
            return super().__call__(args)

    code, _, _ = run(Alternating(), timeout=10_000)
    assert code == pr_wait.GREEN


def test_a_failure_past_the_deadline_raises_at_once() -> None:
    gh = Flaky(1, [[check("a", "pass")]])
    with pytest.raises(RuntimeError, match="gh pr view 276"):
        run(gh, timeout=0)


def test_a_second_required_check_that_is_not_registered_is_not_seen() -> None:
    # Pins the assumption of ``State.required``: gh lists only registered
    # required checks, so "some required check present" equals "every required
    # check" only while branch protection requires one. With a second required
    # check still unregistered the verdict is settled. If this starts failing,
    # the rule was changed: update the comment on ``State.required`` with it.
    # ``State`` is built directly: the fake ``gh`` cannot list a check that is
    # not registered, which is the point.
    gate = check("gate", "pass")
    only_registered = pr_wait.State(HEAD, "OPEN", "CLEAN", "MERGEABLE", [gate], [gate])
    assert only_registered.registered
    # The same rule refuses when no required check is registered at all.
    none_registered = pr_wait.State(HEAD, "OPEN", "CLEAN", "MERGEABLE", [gate], [])
    assert not none_registered.registered
