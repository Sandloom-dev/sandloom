"""Keep PR minute savings distinct from complete pre-release validation."""

from __future__ import annotations

import fnmatch
import importlib.util
import re
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


def test_kernel_boundary_prs_run_on_the_merge_candidate():
    ci = workflow("ci.yml")
    assert set(triggers(ci)["pull_request"]["branches"]) == {
        "main",
        "arch/kernel-boundary",
    }
    # The default pull_request checkout tests refs/pull/<n>/merge, not head.
    for job in ci["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert "ref" not in step.get("with", {})


@pytest.mark.parametrize("base", ["main", "arch/kernel-boundary"])
@pytest.mark.parametrize("full", [False, True])
def test_boundary_gates_run_even_for_a_docs_only_change(runner, base, full):
    outputs = workflow("ci.yml")["jobs"]["changes"]["outputs"]
    context = {
        "github": {"base_ref": base},
        "inputs": {"full_validation": full},
        "steps": {
            step_id: {
                "outputs": dict.fromkeys(
                    [
                        *outputs,
                        *yaml.safe_load(step["with"]["filters"]),
                    ],
                    "false",
                )
            }
            for step_id, step in _filter_steps().items()
        },
    }
    actual = {
        name: runner.Expr(
            expr.removeprefix("${{").removesuffix("}}").strip(), context
        ).eval()
        for name, expr in outputs.items()
    }
    mandatory = {"backend", "examples", "packages", "architecture", "image"}
    assert actual == {
        name: "true"
        if full or (base == "arch/kernel-boundary" and name in mandatory)
        else "false"
        for name in outputs
    }


def test_backend_checks_generated_files_with_the_declared_composition():
    steps = workflow("ci.yml")["jobs"]["backend-static"]["steps"]
    step = next(s for s in steps if s.get("run") == "uv run sandloom generate --check")
    assert step["env"]["SANDLOOM_COMPOSITION_ROOT"] == "sandloom_standard.main"


def test_uv_cache_has_one_writer_per_python_and_explicit_readers(runner):
    writers = []
    for path in WORKFLOWS.glob("*.yml"):
        for name, job in workflow(path.name).get("jobs", {}).items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("astral-sh/setup-uv@"):
                    continue
                inputs = step["with"]
                save = inputs["save-cache"]
                assert isinstance(save, bool) or save == PART_WRITES, (path.name, name)
                if not inputs["enable-cache"]:
                    assert save is False
                    continue
                assert inputs["cache-dependency-glob"] == "uv.lock"
                assert "python-version" in inputs
                if save:
                    writers.append((path.name, name))
                    assert inputs["python-version"] == "${{ matrix.python }}"
                    for full in (False, True):
                        versions = [
                            row["python"]
                            for row in runner._expand_matrix(job, full_validation=full)
                            if runner.eval_if(save, {"matrix": row}, "success")
                        ]
                        assert len(versions) == len(set(versions))
    assert writers == [("ci.yml", "backend-tests")]


#: Of a Python's matrix rows split into parts, only the first saves the cache.
PART_WRITES = "${{ matrix.part != '2/2' }}"


def test_serial_gate_only_omits_probes_still_covered_by_the_full_matrix(runner):
    jobs = workflow("ci.yml")["jobs"]
    serial = jobs["backend-tests-isolation"]
    command = next(
        s["run"] for s in serial["steps"] if "--sandloom-shuffle" in s.get("run", "")
    )
    assert '-m "not subprocess_probe"' in command
    assert "no:xdist" in command and "--durations=25" in command
    # The shards of one seed are disjoint and together the whole selection
    # (sandloom.core.testing.shuffle): every shard of 1..N runs, all with the
    # seed every matrix entry shares, never a seed of its own.
    shards = serial["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1))
    assert serial["strategy"]["fail-fast"] is False
    assert f"--sandloom-shard=${{{{ matrix.shard }}}}/{len(shards)}" in command
    assert "--sandloom-shuffle=${{ github.run_id }}" in command
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
        {
            "python": "3.12",
            "coverage": False,
            "slow": False,
            "part": "1/2",
            "ledger": "part1",
        },
        {
            "python": "3.12",
            "coverage": False,
            "slow": False,
            "part": "2/2",
            "ledger": "part2",
        },
    ]
    # The parts share one seed, so together they are the whole fast suite;
    # a full-validation row is one job with a fresh seed and no part.
    command = next(
        s["run"] for s in job["steps"] if "--sandloom-shuffle" in s.get("run", "")
    )
    assert "--sandloom-shuffle=${{ matrix.part && github.run_id || 'random' }}" in (
        command
    )
    assert "${{ matrix.part && '--sandloom-shard=' || '' }}${{ matrix.part }}" in (
        command
    )
    full = runner._expand_matrix(job, full_validation=True)
    assert {row["python"] for row in full} == {"3.11", "3.12", "3.13", "3.14"}
    assert {row["python"] for row in full if row["slow"]} == {"3.11", "3.12"}
    assert [row["python"] for row in full if row["coverage"]] == ["3.12"]
    # Each row's skip ledger has its own file and artifact (skip-ledger job).
    for rows in (full, runner._expand_matrix(job, full_validation=False)):
        assert len({row["ledger"] for row in rows}) == len(rows)


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
        ("docs/modules/between/contracts.md", {"docs"}),
        ("tools/docs/docs_site.py", {"docs"}),
        ("website/index.html", {"docs"}),
        (".github/branch-protection.json", set()),
        ("tools/packaging/public-source.json", set()),
        # The SQLite lane's own tooling runs the lane (backend).
        ("tools/ci/ci_local.py", {"backend"}),
        ("tools/smoke/quickstart_check.py", {"backend"}),
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
        ("benchmarks/uv.lock", {"benchmarks", "dependency_review"}),
        ("benchmarks/pyproject.toml", {"benchmarks", "dependency_review"}),
        ("benchmarks/ladder/cg/requirements.txt", {"benchmarks", "dependency_review"}),
        (
            "packages/sandloom-identity/pyproject.toml",
            {"backend", "packages", "architecture", "dependency_review"},
        ),
        (
            "tools/checks/check_benchmark_dependencies.py",
            {"backend", "architecture", "benchmarks"},
        ),
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
        # The image area has its own test (it needs a second filter step).
        if not name.startswith("image")
        and any(fnmatch.fnmatchcase(path, p) for p in patterns)
    }
    assert selected == expected


def glob_match(path: str, pattern: str) -> bool:
    """paths-filter's glob: ``*`` stays inside a directory, ``**`` crosses them."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.fullmatch(out, path) is not None


def _filter_steps() -> dict[str, dict]:
    steps = workflow("ci.yml")["jobs"]["changes"]["steps"]
    return {s["id"]: s for s in steps if s.get("id", "").startswith("filter")}


def _filter_selects(step: dict, path: str) -> set[str]:
    """The filters of one paths-filter step that select ``path``: any glob of a
    filter (the default `some`), or every glob of it with `!` ones excluding
    (``predicate-quantifier: every``)."""
    filters = yaml.safe_load(step["with"]["filters"])
    every = step["with"].get("predicate-quantifier") == "every"
    selected = set()
    for name, patterns in filters.items():
        positive = [p for p in patterns if not p.startswith("!")]
        negative = [p[1:] for p in patterns if p.startswith("!")]
        if every:
            hit = all(glob_match(path, p) for p in positive) and not any(
                glob_match(path, p) for p in negative
            )
        else:
            assert not negative, name  # a negated glob selects unrelated files
            hit = any(glob_match(path, p) for p in positive)
        if hit:
            selected.add(name)
    return selected


@pytest.mark.parametrize(
    ("path", "builds_image"),
    [
        # Tests, tooling and policy files: the production image is not rebuilt.
        ("tests/architecture/test_required_ci.py", False),
        ("src/sandloom/core/tests/test_nesting_policy.py", False),
        ("src/sandloom_billing/tests/conftest.py", False),
        ("packages/sandloom-redis/tests/test_cache.py", False),
        ("tools/ci/ci_local.py", False),
        ("tools/checks/prepush_full_tests.sh", False),
        ("tools/packaging/public-source.json", False),
        (".env.test", False),
        ("conftest.py", False),
        ("docs/contributing/ci.md", False),
        # What the Dockerfile copies or runs.
        ("src/sandloom/core/app_factory.py", True),
        ("src/sandloom/core/testing/plugin.py", True),
        ("packages/sandloom-redis/src/sandloom_redis/cache.py", True),
        ("packages/sandloom/pyproject.toml", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("migrations/core/versions/0001_init.py", True),
        ("deploy/docker/Dockerfile", True),
        ("tools/packaging/package_build.py", True),
        ("tools/packaging/packages.json", True),
        ("tools/checks/runtime_inventory.py", True),
        (".github/workflows/ci.yml", True),
    ],
)
def test_the_production_image_is_built_only_for_what_it_contains(path, builds_image):
    steps = _filter_steps()
    selected = _filter_selects(steps["filter"], path) | _filter_selects(
        steps["filter_image"], path
    )
    image_filters = {"image_inputs", "image_src", "image_packages"}
    assert bool(selected & image_filters) is builds_image, selected


def test_docker_build_waits_for_the_image_area_not_for_backend(runner):
    body = workflow("ci.yml")["jobs"]["docker-build"]

    def runs(**outputs: str) -> bool:
        context = {
            "inputs": {"full_validation": False, "reused_jobs": "[]"},
            "needs": {"changes": {"outputs": outputs}},
        }
        return runner.eval_if(body["if"], context, "success")

    assert runs(backend="true", deploy="false", image="false") is False
    assert runs(backend="false", deploy="true", image="false") is False
    assert runs(backend="false", deploy="false", image="true") is True


def test_benchmarks_filter_leaves_out_only_the_dx_harness():
    """The filter lists benchmarks/'s subdirectories one by one (a negated glob
    selects unrelated files): a new one must be listed, `dx` must not be."""
    filters = yaml.safe_load(_filter_steps()["filter"]["with"]["filters"])
    patterns = filters["benchmarks"]
    children = [p for p in (ROOT / "benchmarks").iterdir() if p.is_dir()]
    assert any(p.name == "dx" for p in children)  # the exclusion has a subject
    for child in children:
        probe = f"benchmarks/{child.name}/some/deeper/file.py"
        listed = any(glob_match(probe, p) for p in patterns)
        assert listed is (child.name != "dx"), child.name
    for top_level in ("benchmarks/README.md", "benchmarks/uv.lock"):
        assert any(glob_match(top_level, p) for p in patterns), top_level


def _runs(job: dict) -> str:
    """The job's commands as one line (line continuations and folds joined)."""
    text = "\n".join(step.get("run", "") for step in job["steps"])
    return " ".join(text.replace("\\\n", " ").split())


def test_the_kernel_lane_is_two_parallel_jobs_that_drop_nothing():
    jobs = workflow("ci.yml")["jobs"]
    with_redis = jobs["kernel-without-batteries"]
    without_redis = jobs["kernel-without-redis"]
    # Parallel: both wait for the classifier only, neither for the other.
    assert with_redis["needs"] == without_redis["needs"] == "changes"
    assert with_redis["if"] == without_redis["if"]
    assert "kernel-without-redis" in jobs["ci-required"]["needs"]
    oracle = (
        "src/sandloom/core/tests/test_nesting_policy.py"
        "::test_nested_units_match_the_oracle_at_length"
    )
    first, second = _runs(with_redis), _runs(without_redis)
    # The run with sandloom-redis installed: the hook alone, Redis kept. The
    # 82 s oracle test is deselected here and runs in the other job.
    assert (
        "src/sandloom/core/tests tests/sdk tests/architecture"
        " tests/integration/replaceability" in first
    )
    assert f"--deselect {oracle}" in first
    assert "--deselect" not in second
    assert "-n auto --dist loadfile --durations=25 --sandloom-shuffle=random" in first
    assert "-p tools.ci.no_batteries" in first
    assert "no_redis" not in first
    assert "uninstall" not in first
    # The run without it: the client and both drivers removed, the same
    # suites under the no_redis plugin, then asyncpg removed for SQLite.
    assert "uv pip uninstall" in second
    assert "redis hiredis sandloom-redis sandloom-celery" in second
    # tests/architecture is whole in the other job and in backend-static; here
    # only this lane's own ratchet test.
    assert (
        "src/sandloom/core/tests tests/sdk tests/architecture/test_no_redis.py"
        " tests/integration/replaceability" in second
    )
    assert "tests/architecture " not in second
    assert "-n auto --dist loadfile --no-cov --durations=25" in second
    assert "-p tools.ci.no_batteries -p tools.ci.no_redis" in second
    assert "asyncpg" in second
    assert "src/sandloom/core/tests/test_postgres_extra.py" in second
    assert "test_postgres_extra" not in first


def test_architecture_tests_of_backend_static_run_on_four_workers_with_a_server():
    """Each xdist worker's conftest creates its own test database: without the
    PostgreSQL service the workers crash at collection (the service is not
    for the tests, which keep their own checks)."""
    job = workflow("ci.yml")["jobs"]["backend-static"]
    step = next(s for s in job["steps"] if "tests/architecture" in s.get("run", ""))
    assert "-n 4 --dist loadfile" in step["run"]
    service = job["services"]["postgres"]
    assert service["ports"] == ["5433:5432"]
    # The workers connect with the step's DATABASE_URL, not the job's dummy one
    # (a role the server lacks: every worker crashed on the first PR run).
    env = service["env"]
    assert step["env"]["DATABASE_URL"] == (
        f"postgresql+asyncpg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:5433/{env['POSTGRES_DB']}"
    )


def test_packages_workflow_keeps_only_the_wheel_matrix_of_module_subsets():
    """The repository and infrastructure matrices run in backend-tests (fast
    tests under the root testpaths, not slow); the wheel matrix is slow and
    runs in no other PR job."""
    text = (WORKFLOWS / "packages.yml").read_text()
    assert "tests/integration/module_subsets/test_module_subsets_wheels.py" in text
    assert "pytest tests/integration/module_subsets \\" not in text
    wheels = ROOT / "tests/integration/module_subsets/test_module_subsets_wheels.py"
    assert "pytest.mark.slow" in wheels.read_text()
    for name in ("repository", "infrastructure"):
        body = (
            ROOT / f"tests/integration/module_subsets/test_module_subsets_{name}.py"
        ).read_text()
        assert "pytest.mark.slow" not in body, name


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
    ("areas", "full", "pr", "reused"),
    [
        (set(), False, True, frozenset()),
        ({"docs"}, False, True, frozenset()),
        ({"backend"}, False, True, frozenset()),
        ({"admin"}, False, True, frozenset()),
        ({"examples", "dependency_review"}, False, True, frozenset()),
        ({"deploy"}, False, True, frozenset()),
        ({"image"}, False, True, frozenset()),
        ({"benchmarks"}, False, True, frozenset()),
        ({"packages", "architecture"}, False, True, frozenset()),
        (set(), True, False, frozenset()),
        # A release that reuses a green PR run of the same tree.
        (set(), True, False, frozenset({"backend-static", "examples", "docker-build"})),
    ],
)
def test_gate_agrees_with_actual_github_job_conditions(runner, areas, full, pr, reused):
    import json

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
        "inputs": {"full_validation": full, "reused_jobs": json.dumps(sorted(reused))},
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
    assert all(needs[name]["result"] == "skipped" for name in reused)
    assert gate.check(needs, full_validation=full, pull_request=pr, reused=reused) == []


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
