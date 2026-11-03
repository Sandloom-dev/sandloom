"""A release reuses a green PR run only for the exact tree that run tested."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "owner/repo"
SHA = "m" * 40
HEAD = "h" * 40
TREE = "t" * 40


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/ci/{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = load("required_ci")
evidence = load("pr_evidence")


def pull(**overrides: Any) -> dict[str, Any]:
    return {
        "merged_at": "2026-11-03T12:05:55Z",
        "merge_commit_sha": SHA,
        "base": {"ref": "main"},
        "head": {"sha": HEAD},
        **overrides,
    }


def run(run_id: int = 7, **overrides: Any) -> dict[str, Any]:
    return {
        "id": run_id,
        "event": "pull_request",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "head_sha": HEAD,
        "repository": {"full_name": REPO},
        "created_at": f"2026-11-03T12:0{run_id % 10}:00Z",
        **overrides,
    }


GREEN_JOBS = [
    {"name": "backend-static", "conclusion": "success"},
    {"name": "supply-chain", "conclusion": "success"},
    {"name": "backend-tests (py3.12, not slow)", "conclusion": "success"},
    {"name": "docker-build", "conclusion": "success"},
    {"name": "examples (linkly, examples/linkly)", "conclusion": "success"},
    {"name": "examples (helpdesk, examples/helpdesk)", "conclusion": "success"},
    {"name": "benchmarks-security", "conclusion": "skipped"},
    {"name": "admin-ui / admin", "conclusion": "success"},
]


class FakeGitHub:
    def __init__(
        self,
        *,
        pulls: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        tree: str = TREE,
        jobs: list[dict[str, Any]] | None = None,
        artifact: dict[str, Any] | None = None,
    ) -> None:
        self.pulls = [pull()] if pulls is None else pulls
        self.runs = [run()] if runs is None else runs
        self.tree = tree
        self.jobs = GREEN_JOBS if jobs is None else jobs
        self.artifact = artifact or {"name": "ci-evidence", "expired": False}
        self.downloaded: list[int] = []

    def api(self, path: str) -> Any:
        if path == f"repos/{REPO}/commits/{SHA}/pulls":
            return self.pulls
        if path.startswith(f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha="):
            assert "event=pull_request" in path and "status=success" in path
            return {"workflow_runs": self.runs}
        match = re.fullmatch(rf"repos/{REPO}/actions/runs/(\d+)/(\w+)(\?.*)?", path)
        assert match, path
        if match.group(2) == "artifacts":
            return {"artifacts": [self.artifact]}
        assert match.group(2) == "jobs" and "filter=latest" in (match.group(3) or "")
        return {"jobs": self.jobs}

    def download(self, run_id: int, dest: Path) -> Path:
        self.downloaded.append(run_id)
        out = dest / "evidence.json"
        evidence.record(out, tree=self.tree, sha="x" * 40, head_sha=HEAD)
        return out


def select(github: FakeGitHub) -> list[str]:
    jobs, why = evidence.select(
        github.api, github.download, repo=REPO, sha=SHA, tree=TREE
    )
    assert why
    return jobs


def test_a_green_pr_run_of_the_same_tree_reuses_only_its_successful_jobs():
    assert select(FakeGitHub()) == [
        "backend-static",
        "docker-build",
        "examples",
        "supply-chain",
    ]


@pytest.mark.parametrize(
    "github",
    [
        pytest.param(FakeGitHub(tree="d" * 40), id="main-moved-after-the-pr-run"),
        pytest.param(FakeGitHub(pulls=[]), id="no-pr"),
        pytest.param(
            FakeGitHub(pulls=[pull(merge_commit_sha="o" * 40)]), id="other-merge"
        ),
        pytest.param(FakeGitHub(pulls=[pull(merged_at=None)]), id="not-merged"),
        pytest.param(FakeGitHub(pulls=[pull(base={"ref": "dev"})]), id="not-main"),
        pytest.param(FakeGitHub(runs=[]), id="no-green-run"),
        pytest.param(FakeGitHub(runs=[run(conclusion="failure")]), id="failed"),
        pytest.param(FakeGitHub(runs=[run(event="workflow_dispatch")]), id="event"),
        pytest.param(
            FakeGitHub(runs=[run(path=".github/workflows/full-validation.yml")]),
            id="other-workflow",
        ),
        pytest.param(
            FakeGitHub(runs=[run(repository={"full_name": "fork/repo"})]),
            id="other-repo",
        ),
        pytest.param(FakeGitHub(runs=[run(head_sha="o" * 40)]), id="other-head"),
        pytest.param(
            FakeGitHub(artifact={"name": "ci-evidence", "expired": True}),
            id="expired",
        ),
        pytest.param(
            FakeGitHub(artifact={"name": "backend-sbom", "expired": False}),
            id="no-evidence",
        ),
    ],
)
def test_anything_short_of_the_same_tree_and_a_green_run_reuses_nothing(github):
    assert select(github) == []


def test_a_matrix_job_is_reused_only_when_every_entry_succeeded():
    jobs = [
        {"name": "examples (linkly, examples/linkly)", "conclusion": "success"},
        {"name": "examples (helpdesk, examples/helpdesk)", "conclusion": "failure"},
        {"name": "examples-extra", "conclusion": "success"},
        {"name": "backend-static", "conclusion": "cancelled"},
    ]
    assert evidence.reused_jobs(jobs) == []


def test_the_newest_green_run_wins_and_an_unreadable_artifact_reuses_nothing():
    github = FakeGitHub(runs=[run(3), run(5)])
    select(github)
    assert github.downloaded == [5]

    def broken(run_id: int, dest: Path) -> Path:
        return dest / "missing.json"

    jobs, _ = evidence.select(FakeGitHub().api, broken, repo=REPO, sha=SHA, tree=TREE)
    assert jobs == []


def test_reused_jobs_are_exactly_the_guarded_jobs_with_identical_profiles():
    jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]
    guarded = {
        name
        for name, body in jobs.items()
        if "inputs.reused_jobs" in str(body.get("if", ""))
    }
    assert guarded == set(gate.REUSABLE_JOBS)
    for name in gate.REUSABLE_JOBS:
        guard = f"!contains(fromJSON(inputs.reused_jobs || '[]'), '{name}')"
        assert guard in jobs[name]["if"]
        # A reusable job runs the same commands in both profiles.
        assert "full_validation" not in json.dumps(jobs[name])
    assert not set(gate.REUSABLE_JOBS) & (gate.FULL_JOBS | {"backend-tests"})


def test_the_release_feeds_validate_from_the_plan_and_pr_runs_record_their_tree():
    release = yaml.safe_load((WORKFLOWS / "release.yml").read_text())["jobs"]
    assert release["validate"]["with"] == {
        "full_validation": True,
        "reused_jobs": "${{ needs.plan.outputs.reused_jobs }}",
    }
    plan = release["plan"]
    assert plan["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "actions": "read",
    }
    step = next(s for s in plan["steps"] if s.get("id") == "reuse")
    assert step["run"] == "python3 tools/ci/pr_evidence.py select"
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    required = ci["jobs"]["ci-required"]
    record = next(
        s for s in required["steps"] if "pr_evidence.py record" in s.get("run", "")
    )
    assert record["if"] == "${{ github.event_name == 'pull_request' }}"
    validate = required["steps"][-1]
    assert validate["env"]["CI_REUSED_JOBS"] == "${{ inputs.reused_jobs || '[]' }}"
    # Called without the input (PRs, the manual umbrella), nothing is reused.
    called = ci.get("on", ci.get(True))["workflow_call"]["inputs"]["reused_jobs"]
    assert called["default"] == "[]"


def full_needs(reused: set[str]) -> dict[str, dict[str, Any]]:
    needs: dict[str, dict[str, Any]] = {
        name: {"result": "success"} for name in gate.ALL_JOBS
    }
    for name in [*gate.PR_JOBS, *reused]:
        needs[name]["result"] = "skipped"
    needs["changes"]["outputs"] = dict.fromkeys(gate.AREAS, "true")
    return needs


def test_the_gate_accepts_a_skip_only_for_jobs_the_release_reused():
    reused = frozenset({"backend-static", "examples"})
    needs = full_needs(set(reused))
    full = {"full_validation": True, "pull_request": False}
    assert gate.check(needs, **full, reused=reused) == []
    assert gate.check(needs, **full)
    needs["backend-tests-isolation"]["result"] = "skipped"
    assert gate.check(needs, **full, reused=reused)


@pytest.mark.parametrize(
    ("reused", "full", "pr"),
    [
        ({"backend-tests"}, True, False),
        ({"backend-tests-isolation"}, True, False),
        ({"backend-static"}, False, True),
        ({"backend-static"}, True, True),
    ],
)
def test_the_gate_refuses_reuse_outside_a_release_or_of_unreusable_jobs(
    reused, full, pr
):
    needs = full_needs(set())
    assert gate.check(
        needs, full_validation=full, pull_request=pr, reused=frozenset(reused)
    )
