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


def test_uv_cache_has_one_writer_per_python_and_explicit_readers(runner):
    writers = []
    for path in WORKFLOWS.glob("*.yml"):
        for name, job in workflow(path.name).get("jobs", {}).items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("astral-sh/setup-uv@"):
                    continue
                inputs = step["with"]
                assert isinstance(inputs["save-cache"], bool), (path.name, name)
                if not inputs["enable-cache"]:
                    assert inputs["save-cache"] is False
                    continue
                assert inputs["cache-dependency-glob"] == "uv.lock"
                assert "python-version" in inputs
                if inputs["save-cache"]:
                    writers.append((path.name, name))
                    assert inputs["python-version"] == "${{ matrix.python }}"
                    for full in (False, True):
                        versions = [
                            row["python"]
                            for row in runner._expand_matrix(job, full_validation=full)
                        ]
                        assert len(versions) == len(set(versions))
    assert writers == [("ci.yml", "backend-tests")]


def test_serial_gate_only_omits_probes_still_covered_by_the_full_matrix(runner):
    jobs = workflow("ci.yml")["jobs"]
    serial = jobs["backend-tests-isolation"]
    command = next(
        s["run"] for s in serial["steps"] if "--sandloom-shuffle" in s.get("run", "")
    )
    assert '-m "not subprocess_probe"' in command
    assert "no:xdist" in command and "--durations=25" in command
    parallel = next(
        s["run"]
        for s in jobs["backend-tests"]["steps"]
        if "--sandloom-shuffle" in s.get("run", "")
    )
    assert "subprocess_probe" not in parallel
    full = runner._expand_matrix(jobs["backend-tests"], full_validation=True)
    assert {r["python"] for r in full if r["slow"]} == {"3.11", "3.12"}


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


def test_full_serial_gate_can_start_before_change_classification(runner):
    job = workflow("ci.yml")["jobs"]["backend-tests-isolation"]
    assert not job.get("needs")
    assert runner.eval_if(job["if"], {"inputs": {"full_validation": True}}, "success")
    assert not runner.eval_if(
        job["if"], {"inputs": {"full_validation": False}}, "success"
    )
    assert (
        "backend-tests-isolation" in workflow("ci.yml")["jobs"]["ci-required"]["needs"]
    )


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
        ("docs/concepts/contracts.md", {"docs"}),
        ("tools/docs/docs_site.py", {"docs"}),
        ("website/index.html", {"docs"}),
        (".github/branch-protection.json", set()),
        ("tools/packaging/public-source.json", set()),
        ("tools/ci/ci_local.py", set()),
        (
            "src/sandloom/core/unit_of_work/runtime.py",
            {"backend", "admin", "docs", "packages", "architecture"},
        ),
        (
            "src/sandloom_billing/api.py",
            {"backend", "admin", "docs", "packages", "architecture"},
        ),
        ("tests/architecture/test_api_no_orm_leakage.py", {"backend", "architecture"}),
        ("tools/packaging/package_build.py", {"backend", "packages", "architecture"}),
        ("tools/checks/check_secrets.py", {"backend", "architecture"}),
        ("migrations/core/0001_init.py", {"backend", "packages"}),
        (
            "pyproject.toml",
            {
                "backend",
                "admin",
                "docs",
                "packages",
                "architecture",
                "dependency_review",
            },
        ),
        ("conftest.py", {"backend"}),
        ("admin/src/app/router.tsx", {"admin"}),
        (
            "examples/academy/academy/modules/catalog/api.py",
            {"examples", "docs", "dependency_review"},
        ),
        ("deploy/docker/Dockerfile", {"deploy", "docs"}),
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
        "dependency-review.yml",
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
    publish = release["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"]["id-token"] == "write"
    assert set(publish["needs"]) == {"plan", "validate", "distributions", "prepare"}
    assert publish["if"] == "${{ needs.plan.outputs.publish == 'true' }}"
    assert "id-token" not in release["permissions"]
    assert (
        triggers(release)["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    )
    assert not any(
        step.get("uses", "").startswith("actions/checkout") for step in publish["steps"]
    )


def test_hosted_ci_service_connections_do_not_depend_on_dns():
    """DNS uses the loop's thread executor and can exceed a cache deadline
    under suite load even while Redis is healthy. Service-container ports
    bind to loopback; numeric addresses bypass the resolver entirely.
    """
    import ipaddress
    import re
    from urllib.parse import urlsplit

    sources = [ROOT / ".env.test", *WORKFLOWS.glob("*.yml")]
    checked = 0
    for source in sources:
        for raw in re.findall(
            r"(?:postgresql(?:\+asyncpg)?|rediss?)://[^\s\"']+", source.read_text()
        ):
            host = urlsplit(raw).hostname
            assert host is not None
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pytest.fail(
                    f"{source.name}: local test service {host!r} still depends on DNS"
                )
            assert address.is_loopback, (source.name, host)
            checked += 1
    assert checked >= 10


@pytest.mark.parametrize("explicit_timeout", [None, "0.125"])
def test_explicit_ci_urls_still_seed_missing_test_settings(tmp_path, explicit_timeout):
    """Import the actual bootstrap in a fresh process, as a CI worker does.
    Shell overrides must survive, but supplying DATABASE_URL must not disable
    every unrelated test-profile default.
    """
    import json
    import os
    import subprocess

    from dotenv import dotenv_values

    supplied = {
        "DATABASE_URL": "postgresql+asyncpg://ci:ci@127.0.0.1:5433/seed_regression",
        "CACHE_URL": "redis://127.0.0.1:6380/3",
        "BROKER_URL": "redis://127.0.0.1:6380/4",
    }
    env = {"PATH": os.environ.get("PATH", ""), **supplied}
    if explicit_timeout is not None:
        env["CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS"] = explicit_timeout
    code = f"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('_seed_profile_test', {str(ROOT / "conftest.py")!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
keys = ('DATABASE_URL', 'CACHE_URL', 'BROKER_URL', 'CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS')
print(json.dumps({{key: os.environ.get(key) for key in keys}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    actual = json.loads(result.stdout.splitlines()[-1])
    assert {key: actual[key] for key in supplied} == supplied
    expected = (
        explicit_timeout
        or dotenv_values(ROOT / ".env.test")["CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS"]
    )
    assert actual["CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS"] == expected


@pytest.mark.parametrize(
    ("areas", "full", "pr"),
    [
        (set(), False, True),
        ({"docs"}, False, True),
        ({"backend"}, False, True),
        ({"admin"}, False, True),
        ({"examples", "dependency_review"}, False, True),
        ({"deploy"}, False, True),
        ({"benchmarks"}, False, True),
        ({"packages", "architecture"}, False, True),
        (set(), True, False),
    ],
)
def test_gate_agrees_with_actual_github_job_conditions(runner, areas, full, pr):
    spec = importlib.util.spec_from_file_location(
        "required_ci_policy", ROOT / "tools/ci/required_ci.py"
    )
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    jobs = workflow("ci.yml")["jobs"]
    outputs = {
        area: "true" if full or area in areas else "false" for area in gate.AREAS
    }
    context = {
        "github": {
            "event_name": "pull_request" if pr else "workflow_dispatch",
            "event": {"action": "opened" if pr else ""},
        },
        "inputs": {"full_validation": full},
        "needs": {"changes": {"outputs": outputs}},
    }
    needs = {
        name: {
            "result": "success"
            if runner.eval_if(jobs[name].get("if"), context, "success")
            else "skipped"
        }
        for name in jobs["ci-required"]["needs"]
    }
    needs["changes"]["outputs"] = outputs
    assert gate.check(needs, full_validation=full, pull_request=pr) == []


def test_reusable_suites_have_one_pr_entry_point_and_no_failure_bypass():
    ci = workflow("ci.yml")
    for job in ci["jobs"].values():
        assert job.get("continue-on-error") is not True
        if "uses" not in job:
            continue
        name = job["uses"].rsplit("/", 1)[-1]
        called = workflow(name)
        assert set(triggers(called)) == {"workflow_call"}
        assert all(
            body.get("continue-on-error") is not True
            for body in called["jobs"].values()
        )


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "closed"])
def test_close_event_cannot_emit_skipped_success_under_the_required_name(
    runner, action
):
    job = workflow("ci.yml")["jobs"]["ci-required"]
    expression = job["name"].removeprefix("${{").removesuffix("}}").strip()
    actual = runner.Expr(expression, {"github": {"event": {"action": action}}}).eval()
    assert actual == ("CI cancellation" if action == "closed" else "CI required")
