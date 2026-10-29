"""The merge gate fails closed and respects the existing PR/full profiles."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/ci/required_ci.py"
spec = importlib.util.spec_from_file_location("required_ci", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def planned_needs(*areas, full=False, pr=True):
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = document["jobs"]
    needs = {name: {"result": "skipped"} for name in jobs["ci-required"]["needs"]}
    outputs = {
        area: "true" if full or area in areas else "false" for area in gate.AREAS
    }
    needs["changes"] = {"result": "success", "outputs": outputs}
    needs["workflow-policy"]["result"] = "success"
    for name, selectors in gate.AREA_JOBS.items():
        if any(outputs[area] == "true" for area in selectors):
            needs[name]["result"] = "success"
    for name, area in gate.PR_JOBS.items():
        if pr and outputs[area] == "true":
            needs[name]["result"] = "success"
    for name in gate.FULL_JOBS:
        if full:
            needs[name]["result"] = "success"
    return needs


@pytest.mark.parametrize(
    "areas",
    [
        (),
        ("docs",),
        ("backend", "admin", "packages", "architecture"),
        ("examples", "dependency_review"),
        ("deploy",),
    ],
)
def test_selected_successes_and_unrelated_skips_allow_merge(areas):
    assert (
        gate.check(planned_needs(*areas), full_validation=False, pull_request=True)
        == []
    )


@pytest.mark.parametrize(
    "status", ["failure", "cancelled", "skipped", "neutral", "timed_out", "in_progress"]
)
@pytest.mark.parametrize(
    "job",
    [
        "changes",
        "workflow-policy",
        "backend-tests",
        "admin-ui",
        "packages",
        "docs",
        "architecture",
        "dependency-review",
    ],
)
def test_a_required_check_never_accepts_missing_or_unsuccessful_work(job, status):
    needs = planned_needs(*gate.AREAS)
    needs[job]["result"] = status
    assert gate.check(needs, full_validation=False, pull_request=True)


def test_missing_classifier_output_and_new_unclassified_jobs_block_merge():
    needs = planned_needs()
    del needs["changes"]["outputs"]["backend"]
    assert gate.check(needs, full_validation=False, pull_request=True)
    needs = planned_needs()
    needs["new-suite"] = {"result": "success"}
    assert gate.check(needs, full_validation=False, pull_request=True)
    needs = planned_needs()
    del needs["packages"]
    assert gate.check(needs, full_validation=False, pull_request=True)


def test_full_validation_requires_heavy_jobs_but_not_pr_only_reusable_calls():
    needs = planned_needs(full=True, pr=False)
    assert gate.check(needs, full_validation=True, pull_request=False) == []
    needs["quickstart"]["result"] = "skipped"
    assert gate.check(needs, full_validation=True, pull_request=False)


def test_every_workflow_job_reaches_the_final_gate():
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = document["jobs"]
    required = jobs["ci-required"]
    expected = set(jobs) - {"ci-required"}
    assert set(required["needs"]) == expected == gate.ALL_JOBS
    assert "CI required" in required["name"]
    assert "always()" in required["if"]
    assert "closed" in required["if"]
    assert required["timeout-minutes"] <= 5
    assert "toJSON(needs)" in required["steps"][-1]["env"]["CI_NEEDS"]


def test_branch_protection_requires_the_stable_actions_check_without_bypasses():
    protection = json.loads((ROOT / ".github/branch-protection.json").read_text())
    assert protection["required_status_checks"] == {
        "strict": False,
        "checks": [{"context": "CI required", "app_id": 15368}],
    }
    assert protection["restrictions"] == {
        "users": ["Maryarty"],
        "teams": [],
        "apps": [],
    }
    assert protection["enforce_admins"] is True


def test_cli_fails_on_invalid_input_instead_of_reporting_a_green_check():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={
            **os.environ,
            "CI_NEEDS": "{}",
            "CI_FULL_VALIDATION": "false",
            "CI_PULL_REQUEST": "true",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
