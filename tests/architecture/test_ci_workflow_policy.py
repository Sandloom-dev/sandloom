"""Keep PR minute savings distinct from complete pre-release validation."""

from __future__ import annotations

import fnmatch
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(document: dict) -> dict:
    # PyYAML's YAML 1.1 parser treats the workflow key `on` as True.
    return document.get("on", document.get(True, {}))


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ci_policy_runner", ROOT / "tools/ci/ci_local.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_pr_matrix_is_one_fast_312_and_full_matrix_retains_compatibility(runner):
    job = workflow("ci.yml")["jobs"]["backend-tests"]
    assert runner._expand_matrix(job, full_validation=False) == [
        {"python": "3.12", "coverage": False, "slow": False}
    ]
    full = runner._expand_matrix(job, full_validation=True)
    assert {row["python"] for row in full} == {"3.11", "3.12", "3.13", "3.14"}
    assert {row["python"] for row in full if row["slow"]} == {"3.11", "3.12"}
    assert [row["python"] for row in full if row["coverage"]] == ["3.12"]


@pytest.mark.parametrize(
    "name",
    [
        "backend-tests-isolation",
        "quickstart",
        "app-split",
        "app-recipes",
        "microservices-smoke",
    ],
)
def test_heavy_jobs_are_opt_in_and_still_runnable_locally(runner, name):
    body = workflow("ci.yml")["jobs"][name]
    context = {
        "inputs": {"full_validation": False},
        "needs": {"changes": {"outputs": {"backend": "true", "deploy": "true"}}},
    }
    assert runner.eval_if(body["if"], context, "success") is False
    context["inputs"]["full_validation"] = True
    assert runner.eval_if(body["if"], context, "success") is True
    local = next(j for j in runner.local_jobs(ROOT) if j.spec.job == name)
    assert runner.eval_if(body["if"], local.ctx(), "success") is True
    copied = runner.act_workflow_copy((WORKFLOWS / "ci.yml").read_text(), name)
    assert "inputs.full_validation" not in copied


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/concepts/contracts.md", set()),
        ("tools/docs/docs_site.py", set()),
        ("website/index.html", set()),
        (".github/branch-protection.json", set()),
        ("tools/packaging/public-source.json", set()),
        ("tools/ci/ci_local.py", set()),
        ("src/sandloom/core/unit_of_work/runtime.py", {"backend", "admin"}),
        ("src/sandloom_billing/api.py", {"backend", "admin"}),
        ("tests/architecture/test_api_no_orm_leakage.py", {"backend"}),
        ("tools/packaging/package_build.py", {"backend"}),
        ("tools/checks/check_secrets.py", {"backend"}),
        ("migrations/core/0001_init.py", {"backend"}),
        ("pyproject.toml", {"backend", "admin"}),
        ("admin/src/app/router.tsx", {"admin"}),
        ("examples/academy/academy/modules/catalog/api.py", {"examples"}),
        ("deploy/docker/Dockerfile", {"deploy"}),
        ("benchmarks/tests/test_s5_security.py", {"benchmarks"}),
    ],
)
def test_change_filters_do_not_expand_docs_or_policy_into_backend(path, expected):
    changes = workflow("ci.yml")["jobs"]["changes"]
    step = next(s for s in changes["steps"] if s.get("id") == "filter")
    filters = yaml.safe_load(step["with"]["filters"])
    # Only positive globs: paths-filter's default OR semantics make a negated
    # glob match unrelated files. Native workflow path exclusions differ.
    assert all(not p.startswith("!") for patterns in filters.values() for p in patterns)
    selected = {
        name
        for name, patterns in filters.items()
        if any(fnmatch.fnmatchcase(path, p) for p in patterns)
    }
    assert selected == expected


def test_automatic_checks_do_not_repeat_on_main_push_or_schedule():
    for name in (
        "ci.yml",
        "docs.yml",
        "packages.yml",
        "admin.yml",
        "architecture-check.yml",
        "extraction-e2e.yml",
        "benchmarks.yml",
    ):
        events = triggers(workflow(name))
        assert "push" not in events, name
        assert "schedule" not in events, name


def test_new_commits_and_closed_prs_cancel_obsolete_ci(runner):
    ci = workflow("ci.yml")
    assert "closed" in triggers(ci)["pull_request"]["types"]
    for name in ("workflow-policy", "changes"):
        assert (
            runner.eval_if(
                ci["jobs"][name]["if"],
                {"github": {"event": {"action": "closed"}}},
                "success",
            )
            is False
        )
    for name in (
        "ci.yml",
        "docs.yml",
        "packages.yml",
        "admin.yml",
        "architecture-check.yml",
    ):
        policy = workflow(name)["concurrency"]
        assert (
            runner.eval_if(
                policy["cancel-in-progress"],
                {"github": {"event_name": "pull_request"}},
                "success",
            )
            is True
        )


def test_full_validation_calls_all_suites_without_concurrency_collisions():
    full = workflow("full-validation.yml")
    assert set(triggers(full)) == {"workflow_dispatch"}
    assert full["jobs"]["backend"]["with"] == {"full_validation": True}
    called = {job["uses"].rsplit("/", 1)[-1] for job in full["jobs"].values()}
    assert called == {
        "ci.yml",
        "docs.yml",
        "packages.yml",
        "admin.yml",
        "extraction-e2e.yml",
    }
    assert all("workflow_call" in triggers(workflow(name)) for name in called)
    groups = [workflow(name)["concurrency"]["group"] for name in called]
    assert len(groups) == len(set(groups))
    # The release path still requests full backend validation and cannot upload.
    release = workflow("release.yml")
    assert release["jobs"]["validate"]["with"]["full_validation"] is True
    publish = next(
        s
        for s in release["jobs"]["release"]["steps"]
        if s.get("name", "").startswith("Publish to PyPI")
    )
    assert publish["if"] == "${{ false }}"
