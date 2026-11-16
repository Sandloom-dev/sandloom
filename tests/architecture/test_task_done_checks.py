"""The checks hook on tm's task_done: who it checks and when it lets a call by."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "task_done_checks", ROOT / ".claude/hooks/task_done_checks.py"
)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)
real_run_ci_steps = hook.run_ci_steps


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
    # The CI steps are tested below on their own; the handover tests run none.
    monkeypatch.setattr(hook, "run_ci_steps", lambda top: {})
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


# --- the CI steps of the areas the diff touches --------------------------------

_DOCS = {"docs-code-check-self-test", "docs-code-check", "docs-reference-check"}
_LINT = {"docs-stale-symbols-self-test", "docs-prose-lint-self-test"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/guide/x.md", _DOCS),
        ("tools/docs/docs_code_check.py", _DOCS | _LINT),
        ("src/sandloom/sdk/x.py", _DOCS | {"generate-check"}),
        (
            "src/sandloom/core/x.py",
            _DOCS | {"generate-check", "no-redis-pending"},
        ),
        ("AGENTS.md", _DOCS),
        ("examples/shop/README.md", _DOCS),
        (".claude/hooks/task_done_checks.py", _DOCS),
        ("uv.lock", _DOCS | {"generate-check"}),
        ("tools/ci/no_redis_pending.json", {"generate-check", "no-redis-pending"}),
        (
            "tests/integration/replaceability/t.py",
            {"generate-check", "no-redis-pending"},
        ),
        (
            "packages/sandloom-redis/src/sandloom_redis/x.py",
            {"generate-check", "redis-drivers"},
        ),
        (
            "packages/sandloom-celery/tests/test_x.py",
            {"generate-check", "celery-drivers"},
        ),
        ("tests/unit/test_x.py", {"generate-check"}),
        ("migrations/core/x.py", {"generate-check"}),
        ("guarantees.yml", {"generate-check"}),
        ("architecture_rules.yml", {"generate-check"}),
        ("packages/sandloom-redis/pyproject.toml", {"generate-check", "redis-drivers"}),
        ("CHANGELOG.md", set()),
    ],
)
def test_a_path_selects_the_steps_of_its_ci_areas(
    path: str, expected: set[str]
) -> None:
    assert {s.name for s in hook.selected([path])} == expected


def test_the_selected_steps_keep_the_order_of_the_table() -> None:
    names = [s.name for s in hook.selected(["src/sandloom/core/x.py", "docs/a.md"])]
    assert names == [s.name for s in hook.STEPS if s.name in names]


def _ci_filter(name: str) -> set[str]:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["changes"]["steps"]
    filters = [s["with"]["filters"] for s in steps if s.get("id") == "filter"]
    assert filters, (
        "ci.yml has no step with id `filter` in the `changes` job: "
        "DOCS_PATHS and BACKEND_PATHS of .claude/hooks/task_done_checks.py "
        "follow its filters, update them with the rename"
    )
    return set(yaml.safe_load(filters[0])[name])


def test_the_docs_paths_are_the_docs_filter_of_ci() -> None:
    assert set(hook.DOCS_PATHS) == _ci_filter("docs"), (
        "DOCS_PATHS of .claude/hooks/task_done_checks.py differs from the "
        "`docs` filter of .github/workflows/ci.yml"
    )


def test_the_backend_paths_are_the_backend_filter_of_ci() -> None:
    assert set(hook.BACKEND_PATHS) == _ci_filter("backend"), (
        "BACKEND_PATHS of .claude/hooks/task_done_checks.py differs from the "
        "`backend` filter of .github/workflows/ci.yml"
    )


def test_globs_follow_the_paths_filter() -> None:
    agents = hook._glob("**/AGENTS.md")
    assert agents.match("AGENTS.md") and agents.match("a/b/AGENTS.md")
    assert not agents.match("a/NOT_AGENTS.md")
    assert hook._glob("docs/**").match("docs/a/b.md")
    assert not hook._glob("docs/**").match("docsx/a.md")
    assert not hook._glob("tools/ci/no_redis*").match("tools/ci/no_redis/a.py")
    assert hook._glob("tools/ci/no_redis*").match("tools/ci/no_redis_pending.json")


def _stage(monkeypatch: pytest.MonkeyPatch, changed: list[str], runner, **env: str):
    """Run the stage on a fake diff with ``runner`` standing in for the process."""
    for name in hook._LANE_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hook, "_git", lambda args, cwd, **kw: "\n".join(changed))
    monkeypatch.setattr(hook, "_run_bounded", runner)
    monkeypatch.setattr(hook, "_docs_python", lambda top, env, tmp, timeout: "/py311")
    return real_run_ci_steps("/repo")


def test_each_step_is_recorded_passed_or_failed_by_its_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(argv, cwd, env, timeout):
        broken = "docs_reference.py" in " ".join(argv)
        return (1, "stale page: x\n") if broken else (0, "ok")

    record = _stage(monkeypatch, ["docs/a.md"], runner)
    assert {k: v["status"] for k, v in record.items()} == {
        "docs-code-check-self-test": "Passed",
        "docs-code-check": "Passed",
        "docs-reference-check": "Failed",
    }
    assert "stale page" in record["docs-reference-check"]["tail"]
    assert "tail" not in record["docs-code-check"]


def test_a_timeout_a_missing_tool_and_a_spent_budget_are_not_run_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(argv, cwd, env, timeout):
        raise hook.subprocess.TimeoutExpired(argv, timeout)

    def missing(argv, cwd, env, timeout):
        raise FileNotFoundError(2, "no such file", "uv")

    record = _stage(monkeypatch, ["src/sandloom/sdk/x.py"], timeout)
    assert {v["status"] for v in record.values()} == {"not_run"}
    assert "timed out" in record["generate-check"]["reason"]
    record = _stage(monkeypatch, ["src/sandloom/sdk/x.py"], missing)
    assert {v["status"] for v in record.values()} == {"not_run"}
    assert "uv" in record["generate-check"]["reason"]
    called: list[list[str]] = []
    monkeypatch.setattr(hook, "_run_bounded", lambda *a: called.append(a) or (0, ""))
    assert real_run_ci_steps("/repo", budget=0)["generate-check"] == {
        "status": "not_run",
        "seconds": 0.0,
        "reason": "the stage budget was spent",
    }
    assert called == []


def test_a_step_gets_no_more_time_than_the_budget_has_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []
    monkeypatch.setattr(hook, "STEP_TIMEOUT", 300)
    _stage(
        monkeypatch,
        ["src/sandloom/sdk/x.py"],
        lambda argv, cwd, env, timeout: seen.append(timeout) or (0, ""),
    )
    assert seen and all(t <= 180 for t in seen)
    seen.clear()
    monkeypatch.setattr(hook, "_run_bounded", lambda *a: seen.append(a[3]) or (0, ""))
    real_run_ci_steps("/repo", budget=40)
    assert seen and all(t <= 40 for t in seen)


def test_a_driver_lane_runs_only_with_the_agents_own_test_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[tuple[list[str], dict[str, str]]] = []

    def runner(argv, cwd, env, timeout):
        runs.append((argv, env))
        return 0, ""

    path = ["packages/sandloom-redis/src/sandloom_redis/x.py"]
    record = _stage(monkeypatch, path, runner)
    assert record["redis-drivers"]["status"] == "not_run"
    assert "SANDLOOM_TEST_DATABASE_URL" in record["redis-drivers"]["reason"]
    assert not [argv for argv, _env in runs if "pytest" in argv]
    runs.clear()
    resources = {
        "SANDLOOM_TEST_DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/db",
        "SANDLOOM_TEST_CACHE_URL": "redis://127.0.0.1:2/58",
        "SANDLOOM_TEST_BROKER_URL": "redis://127.0.0.1:2/59",
        "SANDLOOM_TEST_OWNER": "me",
    }
    record = _stage(monkeypatch, path, runner, **resources)
    assert record["redis-drivers"]["status"] == "Passed"
    argv, env = next((a, e) for a, e in runs if "pytest" in a)
    assert "packages/sandloom-redis/tests" in argv
    assert "no:xdist" in argv and "no:cacheprovider" in argv
    assert env["SANDLOOM_REDIS_REQUIRED"] == "1"
    assert env["SANDLOOM_TEST_CACHE_URL"] == "redis://127.0.0.1:2/58"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_driver_lane_that_skipped_its_tests_was_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = dict.fromkeys(hook._LANE_ENV, "x")
    summary = "30 passed, 105 skipped in 5.10s"
    record = _stage(
        monkeypatch,
        ["packages/sandloom-celery/src/x.py"],
        lambda argv, cwd, env, timeout: (0, summary),
        **resources,
    )
    assert record["celery-drivers"]["status"] == "not_run"
    assert "105 tests" in record["celery-drivers"]["reason"]
    # A step that is no lane keeps its pass: only a lane skips for want of servers.
    record = _stage(
        monkeypatch,
        ["docs/a.md"],
        lambda argv, cwd, env, timeout: (0, summary),
    )
    assert {v["status"] for v in record.values()} == {"Passed"}


def test_the_celery_lane_does_not_demand_the_built_wheel() -> None:
    # SANDLOOM_CELERY_REQUIRED=1 makes the tests refuse a source-tree import, and
    # the hook must not replace the agent's .venv with a wheel.
    steps = {s.name: s for s in hook.STEPS}
    assert "SANDLOOM_CELERY_REQUIRED" not in dict(steps["celery-drivers"].env)
    assert dict(steps["redis-drivers"].env) == {"SANDLOOM_REDIS_REQUIRED": "1"}


def test_a_driver_lane_that_fails_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = dict.fromkeys(hook._LANE_ENV, "x")
    record = _stage(
        monkeypatch,
        ["packages/sandloom-celery/src/x.py"],
        lambda argv, cwd, env, timeout: (1, "FAILED test_x - assert 0"),
        **resources,
    )
    assert record["celery-drivers"]["status"] == "Failed"
    assert "FAILED test_x" in record["celery-drivers"]["tail"]


def test_without_a_python_311_the_docs_steps_are_not_run_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(top, env, tmp, timeout):
        raise RuntimeError("Python 3.11 environment: `uv venv` exited 2: none")

    runs: list[list[str]] = []
    record = _stage(
        monkeypatch,
        ["src/sandloom/sdk/x.py"],
        lambda argv, cwd, env, timeout: runs.append(argv) or (0, ""),
    )
    assert record["docs-code-check"]["status"] == "Passed"
    monkeypatch.setattr(hook, "_docs_python", broken)
    runs.clear()
    record = real_run_ci_steps("/repo")
    for name in _DOCS:
        assert record[name]["status"] == "not_run"
        assert "Python 3.11" in record[name]["reason"]
    assert record["generate-check"]["status"] == "Passed"
    assert len(runs) == 1


def test_the_python_311_environment_is_built_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], str, dict[str, str]]] = []
    monkeypatch.setattr(
        hook,
        "_run_bounded",
        lambda argv, cwd, env, timeout: calls.append((argv, cwd, env)) or (0, ""),
    )
    python = hook._docs_python("/repo", {"VIRTUAL_ENV": "/repo/.venv"}, "/tmp/x", 30)
    assert python == "/tmp/x/venv/bin/python"
    venv, sync = calls
    assert venv[0][:4] == ["uv", "venv", "--python", "3.11"]
    assert sync[0][:3] == ["uv", "sync", "--locked"]
    assert "--no-python-downloads" in sync[0] and "--all-extras" in sync[0]
    for _argv, _cwd, env in calls:
        assert env["UV_PROJECT_ENVIRONMENT"] == "/tmp/x/venv"
        assert "VIRTUAL_ENV" not in env


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (FileNotFoundError(2, "x", "uv"), "uv is not installed"),
        (PermissionError(13, "denied"), "uv could not be started: .*denied"),
        (hook.subprocess.TimeoutExpired("uv", 1), "timed out"),
    ],
)
def test_a_python_311_environment_that_cannot_be_built_says_why(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, reason: str
) -> None:
    def runner(argv, cwd, env, timeout):
        raise failure

    monkeypatch.setattr(hook, "_run_bounded", runner)
    with pytest.raises(RuntimeError, match=reason):
        hook._docs_python("/repo", {}, "/tmp/x", 30)
    monkeypatch.setattr(hook, "_run_bounded", lambda *a: (2, "no 3.11 here"))
    with pytest.raises(RuntimeError, match="exited 2: no 3.11 here"):
        hook._docs_python("/repo", {}, "/tmp/x", 30)


def test_a_step_past_its_timeout_is_killed_with_its_children(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = f"sleep 60 & echo $! > {pid_file}; wait"
    started = time.monotonic()
    with pytest.raises(hook.subprocess.TimeoutExpired):
        hook._run_bounded(["sh", "-c", script], str(tmp_path), dict(os.environ), 1)
    assert time.monotonic() - started < 10
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, 9)
    pytest.fail("the step's child outlived the timeout")


def test_a_diff_that_cannot_be_read_is_one_not_run_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(args, cwd, **kw):
        raise hook.subprocess.CalledProcessError(128, ["git", *args], "")

    monkeypatch.setattr(hook, "_git", broken)
    record = real_run_ci_steps("/repo")
    assert list(record) == ["ci-steps"]
    assert record["ci-steps"]["status"] == "not_run"


def test_no_changed_path_of_a_ci_area_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[list[str]] = []
    record = _stage(
        monkeypatch,
        ["CHANGELOG.md", "docs-not/x.md"],
        lambda argv, cwd, env, timeout: runs.append(argv) or (0, ""),
    )
    assert record == {} and runs == []


def test_a_failed_ci_step_is_recorded_like_a_failed_hook_and_blocks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd, **kw: "/repo" if "--show-toplevel" in args else "c0ffee",
    )
    monkeypatch.setattr(hook, "run_checks", lambda top: ({"ruff": "Passed"}, "", 0))
    steps = {
        "docs-code-check": {"status": "Failed", "seconds": 1.0, "tail": "D2: x"},
        "generate-check": {"status": "not_run", "seconds": 0.0, "reason": "r"},
    }
    monkeypatch.setattr(hook, "run_ci_steps", lambda top: steps)
    event = {"agent_type": hook.AGENT, "tool_input": {"task": "t"}, "cwd": "/repo"}
    answer = json.loads(_call(monkeypatch, event))["hookSpecificOutput"]
    deliverable = json.loads(answer["permissionDecisionReason"].splitlines()[-1])
    record = json.loads(deliverable["content"])
    assert record["ci_steps"] == steps
    assert record["failed"] == ["ci:docs-code-check"]
    assert "not_run" not in record  # a step that did not run is no refusal
    event["tool_input"]["deliverables"] = [deliverable]
    assert _call(monkeypatch, event) == ""


def test_ci_steps_are_dropped_with_the_results_when_head_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = iter(["first", "second"])
    monkeypatch.setattr(
        hook,
        "_git",
        lambda args, cwd, **kw: "/repo" if "--show-toplevel" in args else next(heads),
    )
    monkeypatch.setattr(hook, "run_checks", lambda top: ({"ruff": "Passed"}, "", 0))
    monkeypatch.setattr(
        hook,
        "run_ci_steps",
        lambda top: {"docs-code-check": {"status": "Failed", "seconds": 1.0}},
    )
    answer = json.loads(
        _call(monkeypatch, {"agent_type": hook.AGENT, "tool_input": {}})
    )
    record = json.loads(
        json.loads(
            answer["hookSpecificOutput"]["permissionDecisionReason"].splitlines()[-1]
        )["content"]
    )
    assert record["ci_steps"] == {} and record["failed"] == []
    assert record["not_run"] == "HEAD moved while the checks ran"


def test_a_credential_in_a_ci_step_output_is_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stage(
        monkeypatch,
        ["docs/a.md"],
        lambda argv, cwd, env, timeout: (1, "connect redis://:hunter2@host/0 failed"),
    )
    assert "hunter2" not in json.dumps(record)


def test_a_cut_through_a_url_in_a_ci_step_output_never_leaves_its_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whatever the length of what follows the URL, some cut of the output lands
    # inside it: the masking must come before every cut.
    for suffix in range(100, 500, 2):
        output = "p" * 20 + "https://deploy:hunter2secret@example.org/x" + "y" * suffix
        record = _stage(
            monkeypatch,
            ["docs/a.md"],
            lambda argv, cwd, env, timeout, o=output: (1, o),
        )
        assert record["docs-code-check"]["tail"]
        assert "hunter2secret" not in json.dumps(record), suffix


def test_a_cut_through_a_url_in_the_python_311_error_never_leaves_its_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "_git", lambda args, cwd, **kw: "docs/a.md")
    for suffix in range(100, 400, 2):
        output = "p" * 20 + "https://deploy:hunter2secret@example.org/x" + "y" * suffix
        monkeypatch.setattr(hook, "_run_bounded", lambda *a, o=output: (2, o))
        record = real_run_ci_steps("/repo")
        reasons = [v["reason"] for v in record.values()]
        assert reasons and all(v["status"] == "not_run" for v in record.values())
        assert "hunter2secret" not in json.dumps(record), suffix


def test_a_stage_that_raises_is_one_not_run_record_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook, "_git", lambda args, cwd, **kw: "docs/a.md")

    def broken(*a, **kw):
        raise PermissionError(13, "no temp for you")

    monkeypatch.setattr(hook, "_mktemp", broken)
    record = real_run_ci_steps("/repo")
    assert list(record) == ["ci-steps"]
    assert record["ci-steps"]["status"] == "not_run"
    assert "no temp for you" in record["ci-steps"]["reason"]


# --- one deadline for the whole hook, and a SIGTERM that cleans up ----------------


def test_pre_commit_and_the_stage_share_what_the_hook_deadline_has_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []
    monkeypatch.setattr(
        hook,
        "_run_bounded",
        lambda argv, cwd, env, timeout: seen.append(timeout) or (0, ""),
    )
    monkeypatch.setattr(hook, "_deadline_at", time.monotonic() + 100)
    hook.run_checks("/repo")
    assert seen == [pytest.approx(100 - hook.KILL_MARGIN, abs=2)]
    seen.clear()
    monkeypatch.setattr(hook, "_git", lambda args, cwd, **kw: "src/sandloom/sdk/x.py")
    monkeypatch.setattr(hook, "_docs_python", lambda *a: "/py311")
    real_run_ci_steps("/repo")
    # no step may be given more than the stage budget, itself under the deadline
    assert seen and all(t <= 100 - hook.KILL_MARGIN for t in seen)
    # the stage ends before the deadline minus the margin, however long its steps
    monkeypatch.setattr(hook, "_deadline_at", time.monotonic() + 40)
    seen.clear()
    real_run_ci_steps("/repo")
    assert seen and all(t <= 40 - hook.KILL_MARGIN for t in seen)


def test_a_stage_budget_is_capped_when_the_deadline_is_far(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []
    monkeypatch.setattr(hook, "STEP_TIMEOUT", 10_000)
    monkeypatch.setattr(hook, "_deadline_at", time.monotonic() + 10_000)
    monkeypatch.setattr(hook, "_git", lambda args, cwd, **kw: "src/sandloom/sdk/x.py")
    monkeypatch.setattr(hook, "_docs_python", lambda *a: "/py311")
    monkeypatch.setattr(
        hook,
        "_run_bounded",
        lambda argv, cwd, env, timeout: seen.append(timeout) or (0, ""),
    )
    real_run_ci_steps("/repo")
    assert seen and all(t <= hook.STAGE_BUDGET for t in seen)


def test_the_hooks_own_deadline_is_under_its_timeout_in_the_settings() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    timeouts = [
        h["timeout"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
        if "task_done_checks" in h["command"]
    ]
    assert timeouts, "no hook entry runs task_done_checks.py"
    assert all(hook.HOOK_DEADLINE + hook.KILL_MARGIN <= t for t in timeouts)


def test_a_sigterm_kills_the_live_steps_and_removes_the_temp_directory(
    tmp_path: Path,
) -> None:
    import signal
    import subprocess
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()
    git = [
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
    ]
    (repo / "a.md").write_text("a")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "a.md"],
        [*git, "commit", "-q", "-m", "one"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    (repo / "docs").mkdir()
    (repo / "docs/a.md").write_text("docs")
    subprocess.run(["git", "add", "docs/a.md"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "two"], cwd=repo, check=True)
    # a `uv` that records its pid and sleeps: pre-commit and the 3.11 build
    # both start it, each as the leader of a session of its own
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "pids"
    fake = bin_dir / "uv"
    fake.write_text(f"#!/bin/sh\necho $$ >> {pids}\nexec sleep 120\n")
    fake.chmod(0o755)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(temp_root),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / ".claude/hooks/task_done_checks.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdin
    proc.stdin.write(json.dumps({"agent_type": hook.AGENT, "cwd": str(repo)}))
    proc.stdin.close()
    try:
        for _ in range(100):
            started = pids.read_text().split() if pids.exists() else []
            if len(started) >= 2 and list(temp_root.glob("sandloom-ci-steps-*")):
                break
            time.sleep(0.1)
        else:
            pytest.fail("the hook did not start its steps")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    for pid in map(int, started):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            pytest.fail(f"step {pid} outlived the hook")
    assert not list(temp_root.glob("sandloom-ci-steps-*"))


# --- the no-Redis pending list, checked without a venv ---------------------------


def _pending_tree(
    root: Path,
    tests: list[str],
    files: list[str] = (),
    sources: dict[str, str] | None = None,
) -> str:
    (root / "tools/ci").mkdir(parents=True)
    (root / "tools/ci/no_redis.py").write_text(
        (ROOT / "tools/ci/no_redis.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    pending = {"files": {"why": list(files)}, "tests": {"why": list(tests)}}
    (root / "tools/ci/no_redis_pending.json").write_text(json.dumps(pending))
    for rel, text in (sources or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


_SOURCE = (
    "def test_a(): ...\n"
    "async def test_async(): ...\n"
    "class TestC:\n"
    "    def test_m(self): ...\n"
    "    class Inner:\n"
    "        def test_deep(self): ...\n"
)


def test_the_real_pending_list_is_consistent() -> None:
    assert hook.no_redis_problems(str(ROOT)) == ""


def test_a_pending_list_that_names_what_exists_passes(tmp_path: Path) -> None:
    top = _pending_tree(
        tmp_path,
        [
            "t/test_x.py::test_a",
            "t/test_x.py::test_async",
            "t/test_x.py::TestC::test_m[p1]",
            "t/test_x.py::TestC::Inner::test_deep",
        ],
        files=["t/gone_not.py"],
        sources={"t/test_x.py": _SOURCE, "t/gone_not.py": ""},
    )
    assert hook.no_redis_problems(top) == ""


@pytest.mark.parametrize(
    ("tests", "files", "expected"),
    [
        (["t/test_x.py::test_renamed"], [], "stale test: t/test_x.py::test_renamed"),
        (["t/test_x.py::TestC::test_gone"], [], "stale test"),
        (["t/test_missing.py::test_a"], [], "stale test: t/test_missing.py"),
        ([], ["t/test_missing.py"], "gone file: t/test_missing.py"),
        (
            ["t/test_x.py::test_a"] * 1
            + [f"t/test_x.py::test_{i}" for i in range(101)],
            [],
            "ceiling",
        ),
    ],
)
def test_a_pending_list_that_has_gone_stale_or_grown_fails(
    tmp_path: Path, tests: list[str], files: list[str], expected: str
) -> None:
    top = _pending_tree(tmp_path, tests, files, {"t/test_x.py": _SOURCE})
    assert expected in hook.no_redis_problems(top)


def test_an_unparsable_listed_test_file_fails_the_check(tmp_path: Path) -> None:
    top = _pending_tree(
        tmp_path, ["t/test_x.py::test_a"], sources={"t/test_x.py": "def (:\n"}
    )
    assert "unparsable" in hook.no_redis_problems(top)


def test_a_tree_without_the_tool_is_not_run_and_a_broken_tool_fails(
    tmp_path: Path,
) -> None:
    runs = hook._run_step(
        next(s for s in hook.STEPS if s.name == "no-redis-pending"),
        str(tmp_path),
        {},
        "",
        10,
    )
    assert runs[0] == "not_run"
    top = _pending_tree(tmp_path / "t", [])
    (Path(top) / "tools/ci/no_redis.py").write_text("raise RuntimeError('boom')\n")
    status, text = hook._run_step(
        next(s for s in hook.STEPS if s.name == "no-redis-pending"), top, {}, "", 10
    )
    assert status == "Failed" and "boom" in text
