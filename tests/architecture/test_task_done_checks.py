"""The checks hook on tm's task_done: who it checks and when it lets a call by."""

from __future__ import annotations

import importlib.util
import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "task_done_checks", ROOT / ".claude/hooks/task_done_checks.py"
)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _call(monkeypatch: pytest.MonkeyPatch, event: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hook.main() == 0
    return out.getvalue()


def test_pre_commit_results_are_read_per_hook() -> None:
    output = (
        "ruff.....................................................................Passed\n"
        "mypy.....................................................................Failed\n"
        "- hook id: mypy\n"
        "docs-prose-lint.......................................(no files to check)Skipped\n"
    )
    assert hook.results(output) == {
        "ruff": "Passed",
        "mypy": "Failed",
        "docs-prose-lint": "Skipped",
    }


@pytest.fixture(autouse=True)
def _issued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test keeps the hook's issued digests in its own directory."""
    monkeypatch.setattr(hook, "ISSUED", tmp_path / "issued")
    monkeypatch.setattr(hook, "_is_dir", lambda p: True)  # the listed trees are fakes
    return tmp_path / "issued"


def test_a_checks_deliverable_counts_only_for_its_own_commit() -> None:
    record = json.dumps({"commit": "abc"})
    hook.remember(record)
    for raw in (
        [{"kind": "checks", "content": record}],
        json.dumps([{"kind": "checks", "content": record}]),
    ):
        tool_input = {"deliverables": raw}
        assert hook.already_checked(tool_input, "abc")
        assert not hook.already_checked(tool_input, "def")
    assert not hook.already_checked({"deliverables": [{"kind": "git_commit"}]}, "abc")


def test_a_self_written_record_does_not_pass() -> None:
    forged = json.dumps({"commit": "abc", "results": {"ruff": "Passed"}, "failed": []})
    assert not hook.already_checked(
        {"deliverables": [{"kind": "checks", "content": forged}]}, "abc"
    )


def test_a_record_that_is_not_a_string_is_ignored() -> None:
    for content in ({"commit": "abc"}, ["abc"], 7, None):
        assert not hook.already_checked(
            {"deliverables": [{"kind": "checks", "content": content}]}, "abc"
        )


def test_credentials_are_masked_in_the_stored_tail() -> None:
    tail = (
        "connect postgresql+asyncpg://test_user:test_password@127.0.0.1:5433/x failed"
    )
    assert (
        hook.redact(tail) == "connect postgresql+asyncpg://***@127.0.0.1:5433/x failed"
    )
    assert (
        hook.redact("redis://:secret@localhost:6379/0")
        == "redis://***@localhost:6379/0"
    )
    assert (
        hook.redact("fatal: https://ghp_T0KEN@github.com/o/r.git not found")
        == "fatal: https://***@github.com/o/r.git not found"
    )
    assert (
        hook.redact("GET /x?user=a&password=hunter2&api_key=K3Y#f")
        == "GET /x?user=a&password=***&api_key=***#f"
    )
    assert hook.redact("see docs/a.md and ?page=2") == "see docs/a.md and ?page=2"
    assert hook.redact("https://example.test/package") == "https://example.test/package"


def test_a_not_run_text_is_masked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd: "/repo" if "--show-toplevel" in args else "c0ffee",
    )
    monkeypatch.setattr(
        hook,
        "run_checks",
        lambda top: ({}, "fetch https://u:p4ss@example.test/x failed", 1),
    )
    answer = json.loads(
        _call(monkeypatch, {"agent_type": hook.AGENT, "tool_input": {}})
    )
    reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
    record = json.loads(json.loads(reason.splitlines()[-1])["content"])
    assert "p4ss" not in record["not_run"]
    assert "p4ss" not in reason


def test_a_handover_by_anyone_but_an_implementer_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(hook, "run_checks", lambda top: ran.append(top) or ({}, "", 0))
    for agent in (None, "sandloom-reviewer"):
        event = {"tool_name": "mcp__tm__task_done", "tool_input": {}}
        if agent:
            event["agent_type"] = agent
        assert _call(monkeypatch, event) == ""
    assert ran == []


def test_the_first_handover_is_refused_with_the_record_and_the_second_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd: "/repo" if "--show-toplevel" in args else "c0ffee",
    )
    monkeypatch.setattr(
        hook,
        "run_checks",
        lambda top: ({"ruff": "Passed", "mypy": "Failed"}, "error: x", 1),
    )
    event = {"agent_type": hook.AGENT, "tool_input": {"task": "t"}, "cwd": "/repo"}
    answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
    assert answer["permissionDecision"] == "deny"
    deliverable = json.loads(answer["permissionDecisionReason"].splitlines()[-1])
    record = json.loads(deliverable["content"])
    assert deliverable["kind"] == "checks"
    assert record["commit"] == "c0ffee"
    assert record["failed"] == ["mypy"]
    event["tool_input"]["deliverables"] = [deliverable]
    assert _call(monkeypatch, event) == ""


def test_a_run_that_could_not_happen_is_recorded_once_as_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(top: str) -> tuple[dict[str, str], str]:
        raise hook.subprocess.TimeoutExpired("pre-commit", 540)

    monkeypatch.setattr(hook, "_git", lambda args, cwd: "x")
    for broken in (timeout, lambda top: ({}, "uv: command not found", 127)):
        monkeypatch.setattr(hook, "run_checks", broken)
        event = {"agent_type": hook.AGENT, "tool_input": {}}
        answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
        deliverable = json.loads(answer["permissionDecisionReason"].splitlines()[-1])
        record = json.loads(deliverable["content"])
        assert record["results"] == {}
        assert record["not_run"]
        event["tool_input"]["deliverables"] = [deliverable]
        assert _call(monkeypatch, event) == ""


def test_the_settings_route_every_tm_server_name_to_the_hook() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    matchers = [
        entry["matcher"]
        for entry in settings["hooks"]["PreToolUse"]
        if any("task_done_checks.py" in h["command"] for h in entry["hooks"])
    ]
    assert len(matchers) == 1
    pattern = re.compile(matchers[0])
    assert pattern.search("mcp__tm__task_done")
    assert pattern.search("mcp__tm-dev__task_done")
    assert not pattern.search("mcp__tm__task_done_extra")
    assert not pattern.search("mcp__tm__task_start")


def test_an_errored_run_with_passing_hooks_keeps_its_exit_status_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd: "/repo" if "--show-toplevel" in args else "c0ffee",
    )
    monkeypatch.setattr(
        hook, "run_checks", lambda top: ({"ruff": "Passed"}, "Traceback: boom", 3)
    )
    answer = json.loads(
        _call(monkeypatch, {"agent_type": hook.AGENT, "tool_input": {}})
    )
    record = json.loads(
        json.loads(
            answer["hookSpecificOutput"]["permissionDecisionReason"].splitlines()[-1]
        )["content"]
    )
    assert record["exit_status"] == 3
    assert record["failed"] == []
    assert record["tail"] == "Traceback: boom"


def test_results_are_dropped_when_head_moves_during_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = iter(["first", "second"])
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd: "/repo" if "--show-toplevel" in args else next(heads),
    )
    monkeypatch.setattr(hook, "run_checks", lambda top: ({"ruff": "Passed"}, "", 0))
    answer = json.loads(
        _call(monkeypatch, {"agent_type": hook.AGENT, "tool_input": {}})
    )
    record = json.loads(
        json.loads(
            answer["hookSpecificOutput"]["permissionDecisionReason"].splitlines()[-1]
        )["content"]
    )
    assert record["commit"] == "first"
    assert record["results"] == {}
    assert record["not_run"] == "HEAD moved while the checks ran"


def test_a_credential_at_the_cutoff_is_masked_before_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "cut0ff-pass"
    tail = (
        f"https://user:{secret}@example.test/" + "x" * 280
    )  # last 300 start mid-password
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd: "/repo" if "--show-toplevel" in args else "c0ffee",
    )

    def raise_long(top: str) -> tuple[dict[str, str], str, int]:
        raise OSError(
            "x" * 270 + f" redis://:{secret}@localhost:6379/0"
        )  # first 300 end on it

    for run in (lambda top: ({}, tail, 1), raise_long):
        monkeypatch.setattr(hook, "run_checks", run)
        reason = json.loads(
            _call(monkeypatch, {"agent_type": hook.AGENT, "tool_input": {}})
        )["hookSpecificOutput"]["permissionDecisionReason"]
        # Neither the head of the password (cut after it) nor its tail (cut before).
        assert "cut0" not in reason
        assert "f-pass" not in reason


_MAIN_HEAD = "3e13032b" + "0" * 32
_AGENT_HEAD = "60bb2d4a" + "1" * 32
_LISTING = (
    f"worktree /repo\nHEAD {_MAIN_HEAD}\nbranch refs/heads/main\n\n"
    f"worktree /repo/.claude/worktrees/fix\nHEAD {_AGENT_HEAD}\n"
    "branch refs/heads/claude/fix\n"
)


def _fake_git(
    args: list[str], cwd: str, listing: str = _LISTING, *, strict: bool = False
) -> str:
    if args[:2] == ["worktree", "list"]:
        return listing
    if "--verify" in args:
        token = args[-1].removesuffix("^{commit}")
        return next((s for s in (_MAIN_HEAD, _AGENT_HEAD) if s.startswith(token)), "")
    if args[0] == "status" and cwd.endswith("/broken"):
        if strict:
            raise hook.subprocess.CalledProcessError(128, ["git", *args], "")
        return ""
    if args[0] == "status":
        return " M dirty.py" if cwd.endswith("/stale") else ""
    if "--show-toplevel" in args:
        return "/repo"
    return _AGENT_HEAD if cwd.endswith("/fix") else _MAIN_HEAD


def _commit(sha: str) -> dict:
    return {"kind": "git_commit", "content": f"{sha} on claude/fix, PR #254"}


def test_the_handed_over_commit_picks_the_agents_worktree_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(hook, "_git", _fake_git)
    monkeypatch.setattr(
        hook, "run_checks", lambda top: ran.append(top) or ({"ruff": "Passed"}, "", 0)
    )
    event = {
        "agent_type": hook.AGENT,
        "cwd": "/repo",  # an agent that made its own worktree still reports this
        "tool_input": {"deliverables": [_commit(_AGENT_HEAD)]},
    }
    answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
    record = json.loads(
        json.loads(answer["permissionDecisionReason"].splitlines()[-1])["content"]
    )
    assert ran == ["/repo/.claude/worktrees/fix"]
    assert record["commit"] == _AGENT_HEAD
    assert record["results"] == {"ruff": "Passed"}


def test_a_commit_no_worktree_holds_is_recorded_not_run_without_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    missing = "deadbeef" * 5
    monkeypatch.setattr(hook, "_git", _fake_git)
    monkeypatch.setattr(hook, "run_checks", lambda top: ran.append(top) or ({}, "", 0))
    event = {
        "agent_type": hook.AGENT,
        "cwd": "/repo",
        "tool_input": {"deliverables": [_commit(missing)]},
    }
    answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
    deliverable = json.loads(answer["permissionDecisionReason"].splitlines()[-1])
    record = json.loads(deliverable["content"])
    assert ran == []
    assert record["commit"] == missing
    assert record["results"] == {}
    assert "no usable worktree" in record["not_run"]
    event["tool_input"]["deliverables"].append(deliverable)
    assert _call(monkeypatch, event) == ""


def test_implementers_run_in_their_own_worktree() -> None:
    text = (ROOT / ".claude/agents/sandloom-implementer.md").read_text()
    frontmatter = text.split("---")[1]
    assert re.search(r"^isolation: worktree$", frontmatter, re.MULTILINE)


def _checked_tree(monkeypatch: pytest.MonkeyPatch, git, deliverable: dict):
    ran: list[str] = []
    monkeypatch.setattr(hook, "_git", git)
    monkeypatch.setattr(
        hook, "run_checks", lambda top: ran.append(top) or ({"ruff": "Passed"}, "", 0)
    )
    event = {
        "agent_type": hook.AGENT,
        "cwd": "/repo",
        "tool_input": {"deliverables": [deliverable]},
    }
    answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
    record = json.loads(
        json.loads(answer["permissionDecisionReason"].splitlines()[-1])["content"]
    )
    return ran, record


def test_an_abbreviated_sha_is_resolved_before_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran, record = _checked_tree(monkeypatch, _fake_git, _commit(_AGENT_HEAD[:8]))
    assert ran == ["/repo/.claude/worktrees/fix"]
    assert record["commit"] == _AGENT_HEAD


def test_a_git_commit_naming_no_commit_is_not_run_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran, record = _checked_tree(
        monkeypatch, _fake_git, {"kind": "git_commit", "content": "PR #254 (abcdefg)"}
    )
    assert ran == []
    assert record["results"] == {}
    assert "names no commit" in record["not_run"]


def test_a_clean_worktree_wins_over_a_dirty_one_at_the_same_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = (
        f"worktree /repo\nHEAD {_MAIN_HEAD}\nbranch refs/heads/main\n\n"
        f"worktree /repo/.claude/worktrees/stale\nHEAD {_AGENT_HEAD}\n"
        "branch refs/heads/claude/fix-old\n\n"
        f"worktree /repo/.claude/worktrees/fix\nHEAD {_AGENT_HEAD}\n"
        "detached\n"
    )
    ran, _ = _checked_tree(
        monkeypatch,
        lambda args, cwd, **kw: _fake_git(args, cwd, listing, **kw),
        _commit(_AGENT_HEAD),
    )
    assert ran == ["/repo/.claude/worktrees/fix"]


def test_a_later_resolvable_sha_wins_over_an_unknown_full_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = f"{'deadbeef' * 5} then {_AGENT_HEAD[:9]}"
    ran, record = _checked_tree(
        monkeypatch, _fake_git, {"kind": "git_commit", "content": content}
    )
    assert ran == ["/repo/.claude/worktrees/fix"]
    assert record["commit"] == _AGENT_HEAD


def test_a_listed_worktree_that_is_gone_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = (
        f"worktree /repo/.claude/worktrees/gone\nHEAD {_AGENT_HEAD}\ndetached\n\n"
        f"worktree /repo/.claude/worktrees/fix\nHEAD {_AGENT_HEAD}\ndetached\n"
    )
    monkeypatch.setattr(hook, "_is_dir", lambda p: not p.endswith("/gone"))
    ran, _ = _checked_tree(
        monkeypatch,
        lambda args, cwd, **kw: _fake_git(args, cwd, listing, **kw),
        _commit(_AGENT_HEAD),
    )
    assert ran == ["/repo/.claude/worktrees/fix"]


def test_an_unresolved_handover_is_not_satisfied_by_the_cwds_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "_git", _fake_git)
    cwd_record = json.dumps({"commit": _MAIN_HEAD})
    hook.remember(cwd_record)
    event = {
        "agent_type": hook.AGENT,
        "cwd": "/repo",
        "tool_input": {
            "deliverables": [
                {"kind": "git_commit", "content": "PR #254 (abcdefg)"},
                {"kind": "checks", "content": cwd_record},
            ]
        },
    }
    answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
    deliverable = json.loads(answer["permissionDecisionReason"].splitlines()[-1])
    record = json.loads(deliverable["content"])
    assert record["commit"].startswith("unresolved:")
    assert "names no commit" in record["not_run"]
    event["tool_input"]["deliverables"].append(deliverable)
    assert _call(monkeypatch, event) == ""


def test_a_worktree_whose_status_fails_is_skipped_and_none_left_is_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = f"worktree /repo/.claude/worktrees/broken\nHEAD {_AGENT_HEAD}\ndetached\n"
    fix = f"worktree /repo/.claude/worktrees/fix\nHEAD {_AGENT_HEAD}\ndetached\n"
    ran, _ = _checked_tree(
        monkeypatch,
        lambda args, cwd, **kw: _fake_git(args, cwd, broken + "\n" + fix, **kw),
        _commit(_AGENT_HEAD),
    )
    assert ran == ["/repo/.claude/worktrees/fix"]
    ran, record = _checked_tree(
        monkeypatch,
        lambda args, cwd, **kw: _fake_git(args, cwd, broken, **kw),
        _commit(_AGENT_HEAD),
    )
    assert ran == []
    assert "no usable worktree" in record["not_run"]
