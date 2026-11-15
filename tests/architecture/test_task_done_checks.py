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


def test_a_checks_deliverable_counts_only_for_its_own_commit() -> None:
    record = json.dumps({"commit": "abc"})
    for raw in (
        [{"kind": "checks", "content": record}],
        json.dumps([{"kind": "checks", "content": record}]),
    ):
        tool_input = {"deliverables": raw}
        assert hook.already_checked(tool_input, "abc")
        assert not hook.already_checked(tool_input, "def")
    assert not hook.already_checked({"deliverables": [{"kind": "git_commit"}]}, "abc")


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
