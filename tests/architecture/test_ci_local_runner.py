"""Repo contract: the local Docker CI runner still maps the CI workflows.

``tools/ci/ci_local.py`` (the GitHub Actions stand-in, see
``docs/contributing/local-ci.md``) runs the real ``ci.yml`` / ``docs.yml`` /
``architecture-check.yml`` jobs under act, or executes their ``run:`` steps
itself for the jobs act cannot run on a dev host. A workflow change it cannot
map — a new job, a new ``uses:`` action, a new ``${{ }}`` expression or
service image — would make local runs silently diverge from CI, so it fails
here. A changed job body (``FINGERPRINTS``) fails too: review the mapping in
``tools/ci/ci_local.py`` and update the fingerprint printed by
``tools/ci/ci_local.py --check``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "ci" / "ci_local.py"


@pytest.fixture(autouse=True)
def _foreign_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Git hooks export repository-local variables even into temporary checkouts.
    names = subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"], text=True
    ).splitlines()
    for name in names:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def ci_local() -> ModuleType:
    if not SCRIPT.exists() or not (REPO / ".github" / "workflows" / "ci.yml").exists():
        pytest.skip("not a repository checkout (no tools/ci/ci_local.py or ci.yml)")
    spec = importlib.util.spec_from_file_location("ci_local", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_local"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("ci_local", None)
    return module


def test_workflows_are_mapped(ci_local: ModuleType) -> None:
    errors, _ = ci_local.check_workflows(REPO)
    assert not errors, "tools/ci/ci_local.py cannot map the workflows:\n" + "\n".join(
        errors
    )


def test_fallback_export_has_an_independent_tracked_file_index(
    ci_local: ModuleType, tmp_path: Path
) -> None:
    checkout = tmp_path / "export"
    checkout.mkdir()
    (checkout / ".gitignore").write_text("ignored.txt\n.venv/\n")
    (checkout / "source.py").write_text("value = 1\n")
    # A tracked source file can match .gitignore; archive retains it.
    (checkout / "ignored.txt").write_text("committed source\n")
    ci_local.index_export(checkout)
    (checkout / ".venv").mkdir()
    (checkout / ".venv" / "generated.txt").write_text("installed after checkout\n")
    tracked = subprocess.check_output(
        ["git", "-C", str(checkout), "ls-files", "-z"], text=True
    ).split("\0")
    assert set(tracked) - {""} == {".gitignore", "source.py", "ignored.txt"}
    assert (
        subprocess.check_output(["git", "-C", str(checkout), "remote"], text=True) == ""
    )
    assert subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"], text=True
    ).strip() == str(checkout)


def test_workflow_jobs_unchanged_since_review(ci_local: ModuleType) -> None:
    _, warnings = ci_local.check_workflows(REPO)
    assert not warnings, (
        "a CI workflow job changed: review tools/ci/ci_local.py (CI_JOBS / KNOWN_USES / "
        "fallback runner) and update FINGERPRINTS (`tools/ci/ci_local.py --check`):\n"
        + "\n".join(warnings)
    )


def test_expression_evaluator(ci_local: ModuleType) -> None:
    ctx: dict[str, dict[str, object]] = {
        "matrix": {"coverage": False, "python": "3.11"}
    }
    assert (
        ci_local.interpolate("${{ matrix.coverage && ' ' || '--no-cov' }}", ctx)
        == "--no-cov"
    )
    ctx["matrix"]["coverage"] = True
    assert (
        ci_local.interpolate("${{ matrix.coverage && 'sysmon' || '' }}", ctx)
        == "sysmon"
    )
    assert (
        ci_local.eval_if("${{ always() && matrix.coverage }}", ctx, "failure") is True
    )
    assert ci_local.eval_if("failure()", ctx, "success") is False
    assert ci_local.eval_if(None, ctx, "failure") is False


def test_act_log_classification(ci_local: ModuleType) -> None:
    changes_ok = (
        "[Monorepo CI/changes]   \u2705  Success - Main actions/checkout@v4 [0.4s]\n"
        "[Monorepo CI/changes] \U0001f3c1  Job succeeded\n"
    )
    # the job under test was skipped by the paths filter: only `changes` ran
    assert ci_local.parse_act_log(changes_ok, 0)[0] == "skipped"
    ran = changes_ok + (
        "[Monorepo CI/deploy-profiles] \u2b50 Run Main docker compose config\n"
        "[Monorepo CI/deploy-profiles]   \u2705  Success - Main docker compose config [173ms]\n"
        "[Monorepo CI/deploy-profiles] \U0001f3c1  Job succeeded\n"
    )
    status, steps = ci_local.parse_act_log(ran, 0)
    assert status == "pass"
    assert steps == [("Main docker compose config", "ok", 0.173)]
    failed = ran.replace("Job succeeded", "Job failed")
    assert ci_local.parse_act_log(failed, 1)[0] == "fail"


def test_compose_port_parsing(ci_local: ModuleType) -> None:
    assert ci_local.parse_compose_port("127.0.0.1:55001\n") == "127.0.0.1:55001"
    assert ci_local.parse_compose_port("0.0.0.0:55001\n[::]:55001\n") == (
        "127.0.0.1:55001"
    )
    assert ci_local.parse_compose_port("[::1]:55001\n") == "[::1]:55001"
    with pytest.raises(RuntimeError):
        ci_local.parse_compose_port("")
    with pytest.raises(RuntimeError):
        ci_local.parse_compose_port(":0\n")


def test_gateway_port_is_allocated_by_docker(ci_local: ModuleType) -> None:
    """The runner never picks the gateway's host port (pick-then-bind races):
    the profile's mapping must take a host-ip-prefixed port 0 verbatim, so
    Docker allocates the port when it binds, and the smoke's URL is rewritten
    to the address read back from docker."""
    compose = (REPO / "deploy" / "compose/microservices.yml").read_text()
    assert '"${GATEWAY_PORT:-8080}:8080"' in compose
    mapping = f"{ci_local.GATEWAY_EPHEMERAL_PORT}:{ci_local.GATEWAY_CONTAINER_PORT}"
    assert mapping == "127.0.0.1:0:8080"
    assert not hasattr(ci_local, "free_port")
    assert all(
        "{GATEWAY_ADDR}" in new
        for new in (n for _, n in ci_local.HOST_RUN_REWRITES["microservices-smoke"])
    )


def test_timeout_scale_is_opt_in(ci_local: ModuleType) -> None:
    env = ci_local.TIMEOUT_SCALE_ENV
    assert ci_local.timeout_scale({}) == 1
    assert ci_local.timeout_scale({env: ""}) == 1
    assert ci_local.timeout_scale({env: "2.5"}) == 2.5
    for bad in ("0.5", "11", "fast"):
        with pytest.raises(SystemExit):
            ci_local.timeout_scale({env: bad})
    assert ci_local.timeout_scale_note(1) == ""
    assert ci_local.timeout_scale_note(3) == f"timeouts x3 ({env})"


def test_subnet_falls_back_when_stale_networks_hold_172_30_31(
    ci_local: ModuleType,
) -> None:
    """Stale /16 networks of other runs on 172.30 and 172.31 must not stop the
    microservices smoke: the picker moves on to the next private pool."""
    import ipaddress

    held = [
        ipaddress.ip_network("172.30.0.0/16"),
        ipaddress.ip_network("172.31.0.0/16"),
    ]
    net = ci_local.pick_subnet(held)
    assert net.prefixlen == 24 and net.subnet_of(ipaddress.ip_network("10.212.0.0/14"))
    free = ci_local.pick_subnet([ipaddress.ip_network("172.30.0.0/16")])
    assert free.subnet_of(ipaddress.ip_network("172.31.0.0/16"))
    with pytest.raises(RuntimeError, match="no free /24"):
        ci_local.pick_subnet(
            [*held, ipaddress.ip_network("10.212.0.0/14")],
        )


def test_stack_addresses_keep_gateway_outside_dynamic_pool(
    ci_local: ModuleType,
) -> None:
    import ipaddress

    subnet, gw, pool = ci_local.stack_addresses(ipaddress.ip_network("172.30.7.0/24"))
    assert (subnet, gw, pool) == ("172.30.7.0/24", "172.30.7.10", "172.30.7.128/25")
    assert ipaddress.ip_address(gw) not in ipaddress.ip_network(pool)


def test_default_parallel_follows_the_docker_engine(ci_local: ModuleType) -> None:
    gib = 1024**3
    assert ci_local.default_parallel(10, 8 * gib) == 3  # Docker Desktop default
    assert ci_local.default_parallel(16, 32 * gib) == 3  # capped
    assert ci_local.default_parallel(4, 8 * gib) == 1
    assert ci_local.default_parallel(10, 4 * gib) == 1  # memory-bound
    assert ci_local.default_parallel(2, 2 * gib) == 1  # never 0


def test_reserved_slot_cpuset_is_not_shared(ci_local: ModuleType) -> None:
    sets = [set(ci_local.cpuset_for(slot, 4, 10)) for slot in range(3)]
    assert sets[0] == {0, 1, 2, 3}
    assert all(len(s) == 4 and not (s & sets[0]) for s in sets[1:])
    # too small an engine for a reserved slot: plain wrap-around
    assert ci_local.cpuset_for(1, 4, 6) == [4, 5, 0, 1]
    free = [0, 1, 2]
    assert ci_local.take_slot(free, timing_sensitive=False) == 2
    assert ci_local.take_slot(free, timing_sensitive=True) == 0
    assert ci_local.take_slot(free, timing_sensitive=False) == 1


def test_schedule_starts_timing_sensitive_then_longest(ci_local: ModuleType) -> None:
    jobs = ci_local.local_jobs(REPO)
    order = [j.name for j in ci_local.schedule(jobs, {"docs": 9999})]
    assert order[0] == "benchmarks-security"
    assert order[1] == "docs"  # measured history beats the static estimate
    assert sorted(order) == sorted(j.name for j in jobs)


def test_rerun_failed_reads_the_newest_run_of_the_ref(
    ci_local: ModuleType, tmp_path: Path
) -> None:
    import json

    def summary(run_id: str, ref: str, **jobs: str) -> None:
        (tmp_path / run_id).mkdir()
        (tmp_path / run_id / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "ref": ref,
                    "jobs": [
                        {"name": n, "status": s, "seconds": 1.0}
                        for n, s in jobs.items()
                    ],
                }
            )
        )

    assert ci_local.last_failed_jobs(tmp_path, "HEAD") is None
    summary("20261025-100000-aaaa", "HEAD", docs="fail", backend_static="pass")
    summary("20261025-110000-bbbb", "other", docs="pass")
    summary(
        "20261025-120000-cccc", "HEAD", docs="pass", examples="error", smoke="cancelled"
    )
    assert ci_local.last_failed_jobs(tmp_path, "HEAD") == (
        "20261025-120000-cccc",
        ["examples", "smoke"],
    )
    assert ci_local.last_failed_jobs(tmp_path, "other") == ("20261025-110000-bbbb", [])
    assert ci_local.job_history(tmp_path)["docs"] == 1.0


def _artifact_steps(doc: dict) -> list[tuple[str, int, dict]]:
    return [
        (job, i, step)
        for job, body in doc["jobs"].items()
        for i, step in enumerate(body.get("steps") or [])
        if str(step.get("uses", "")).split("@")[0]
        in ("actions/upload-artifact", "actions/download-artifact")
    ]


@pytest.mark.parametrize("workflow", ["docs.yml", "ci.yml"])
def test_act_emulates_artifact_actions_without_a_server(
    ci_local: ModuleType, workflow: str
) -> None:
    """act has no artifact backend locally (upload-artifact@v4 timed out on
    CreateArtifact and failed the docs job). An act job's private workflow
    copy runs each artifact step as a local `run:` step; its `with:` becomes
    the step's `env:` and nothing else in the workflow changes."""
    import yaml

    text = (REPO / ".github" / "workflows" / workflow).read_text()
    orig = yaml.safe_load(text)
    copy = yaml.safe_load(ci_local.act_workflow_copy(text, "job", rename=False))
    assert copy["name"] == orig["name"]
    replaced = _artifact_steps(orig)
    assert replaced, f"{workflow} has no artifact step to emulate"
    assert not _artifact_steps(copy)
    for job, i, step in replaced:
        local = copy["jobs"][job]["steps"][i]
        assert "uses" not in local and "with" not in local
        assert "ci_local" in local["run"]
        assert local["env"] == {
            f"INPUT_{k.upper().replace('-', '_')}": v for k, v in step["with"].items()
        }
        restored = {k: v for k, v in local.items() if k not in ("run", "env")}
        assert restored == {k: v for k, v in step.items() if k not in ("uses", "with")}
        copy["jobs"][job]["steps"][i] = step
    assert copy == orig


def test_act_jobs_with_artifacts_run_a_private_copy(ci_local: ModuleType) -> None:
    """Jobs using artifact actions (docs, supply-chain) or `needs` run the
    private copy; act is never handed an artifact server."""
    jobs = {j.name: j for j in ci_local.local_jobs(REPO) if j.spec.engine == "act"}
    assert ci_local.uses_artifacts(jobs["docs"].body)
    assert ci_local.uses_artifacts(jobs["supply-chain"].body)
    assert not ci_local.uses_artifacts(jobs["backend-static"].body)
    assert "--artifact-server-path" not in SCRIPT.read_text()


def _emulate(
    ci_local: ModuleType, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    import yaml

    text = (
        "name: W\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: Upload\n        uses: actions/upload-artifact@v4\n"
        "        with:\n          name: art\n          path: x\n"
    )
    step = yaml.safe_load(ci_local.act_workflow_copy(text, "j", rename=False))["jobs"][
        "j"
    ]["steps"][0]
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
    )


def test_artifact_emulation_lists_files_and_honours_if_no_files_found(
    ci_local: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("ok")
    found = _emulate(
        ci_local, tmp_path, {"INPUT_NAME": "docs", "INPUT_PATH": "site\nmissing"}
    )
    assert found.returncode == 0, found.stdout + found.stderr
    assert "site" in found.stdout and "docs" in found.stdout
    none = _emulate(ci_local, tmp_path, {"INPUT_NAME": "docs", "INPUT_PATH": "missing"})
    assert none.returncode == 0 and "::warning::" in none.stdout
    strict = _emulate(
        ci_local,
        tmp_path,
        {"INPUT_PATH": "missing", "INPUT_IF_NO_FILES_FOUND": "error"},
    )
    assert strict.returncode != 0 and "::error::" in strict.stdout


def test_act_retries_only_a_daemon_error_before_any_step(ci_local: ModuleType) -> None:
    """Docker Desktop under load can fail act's container setup; that attempt
    (and only that kind) is retried once. A failure once any workflow step ran
    is a real result and is never retried away."""
    setup = (
        "[Monorepo CI/changes] ⭐ Run Set up job\n"
        "[Monorepo CI/changes]   🐳  docker run image=catthehacker/ubuntu:act-latest\n"
        "[Monorepo CI/changes] failed to copy content to container: Error response "
        "from daemon: RWLayer of container d85 is unexpectedly nil\n"
        "[Monorepo CI/changes]   ❌  Failure - Set up job\n"
        "[Monorepo CI/changes] 🏁  Job failed\n"
    )
    assert ci_local.act_setup_infra_failure(setup)
    step_ran = setup.replace(
        "⭐ Run Set up job\n", "⭐ Run Set up job\n[x/y] ⭐ Run Main Linting\n"
    )
    assert not ci_local.act_setup_infra_failure(step_ran)
    pre_ran = setup + "[x/y] ⭐ Run Pre actions/cache@v4\n"
    assert not ci_local.act_setup_infra_failure(pre_ran)
    # a step failing on its own (no daemon error) is not infrastructure
    assert not ci_local.act_setup_infra_failure(
        "[x/y] ⭐ Run Main Tests\n[x/y]   ❌  Failure - Main Tests\n"
    )
    # a daemon error reported outside job setup is not retried either
    assert not ci_local.act_setup_infra_failure(
        "[x/y] ⭐ Run Main Build\nError response from daemon: no space left\n"
    )
    assert ci_local.ACT_SETUP_RETRIES == 1


def test_act_jobs_with_needs_get_a_unique_workflow_name(ci_local: ModuleType) -> None:
    """act names containers from the workflow name + job name: every job that
    `needs: changes` would run its `changes` job in the SAME container name.
    The private copy differs from the workflow in its top-level name only."""
    import yaml

    text = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    a = ci_local.act_workflow_copy(text, "backend-static")
    b = ci_local.act_workflow_copy(text, "docker-build")
    local = ci_local.act_workflow_copy(text, "backend-static", rename=False)
    da, db, orig = yaml.safe_load(a), yaml.safe_load(b), yaml.safe_load(local)
    assert da["name"] == f"{orig['name']} [backend-static]" != db["name"]
    assert {k: v for k, v in da.items() if k != "name"} == {
        k: v for k, v in orig.items() if k != "name"
    }


def test_slow_suites_run_on_311_and_312_only(ci_local: ModuleType) -> None:
    """Owner decision (2026-10-25): the `slow` multi-process suites run on the
    supported floor and the coverage run; 3.13 / 3.14 run `-m "not slow"`."""
    commands = {}
    for job in ci_local.local_jobs(REPO):
        if job.spec.local != "backend-tests":
            continue
        step = next(
            s for s in job.body["steps"] if s.get("name", "").startswith("Full test")
        )
        commands[job.matrix_python] = " ".join(
            ci_local.interpolate(step["run"], job.ctx()).split()
        )
    assert set(commands) == {"3.11", "3.12", "3.13", "3.14"}
    for python in ("3.11", "3.12"):
        assert "-m" not in commands[python].split(), commands[python]
    for python in ("3.13", "3.14"):
        assert commands[python].endswith('-m "not slow"'), commands[python]
    assert "--cov" in commands["3.12"]


def _runnable_apps() -> set[str]:
    """Every app in the repository an owner runs ``sandloom test`` in: each
    ``examples/*`` and ``docs/tutorial-app/*`` directory with an
    ``architecture_rules.yml`` (what ``sandloom new`` writes)."""
    patterns = (
        "examples/*/architecture_rules.yml",
        "docs/tutorial-app/*/architecture_rules.yml",
    )
    return {
        str(rules.parent.relative_to(REPO))
        for pattern in patterns
        for rules in REPO.glob(pattern)
    }


def test_every_example_and_the_tutorial_app_run_their_own_gate_in_ci(
    ci_local: ModuleType,
) -> None:
    """The CI `examples` job runs, in every example app and the tutorial app,
    the gate an app developer runs (`sandloom test --ci --full`: generated files,
    schema & deploy, the full suite, ruff check / format, mypy, `sandloom check`,
    snapshots, migration drift), so an app that fails its own gate fails CI.
    A new example or tutorial app fails here until the matrix lists it."""
    apps: dict[str, str] = {}
    for job in ci_local.local_jobs(REPO):
        if job.spec.local != "examples":
            continue
        ctx = job.ctx()
        workdir = job.body["defaults"]["run"]["working-directory"]
        path = ci_local.interpolate(workdir, ctx).removeprefix("./")
        commands = [
            " ".join(ci_local.interpolate(step["run"], ctx).split())
            for step in job.body["steps"]
            if "run" in step and "working-directory" not in step
        ]
        assert "$BIN/sandloom test --ci --full" in commands, (path, commands)
        apps[path] = job.name
    assert set(apps) == _runnable_apps()
    assert len(set(apps.values())) == len(apps), apps  # distinct local job names


@pytest.mark.parametrize("prefixed", [False, True])
def test_export_does_not_inherit_a_hook_repository(
    ci_local: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefixed: bool,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "--quiet", str(parent)], check=True)
    (parent / "keep.txt").write_text("original source")
    subprocess.run(["git", "-C", str(parent), "add", "keep.txt"], check=True)
    clean = dict(os.environ)
    monkeypatch.setenv("GIT_DIR", str(parent / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(parent / ".git/hook-index"))
    checkout = tmp_path / "export"
    checkout.mkdir()
    (checkout / "exported.txt").write_text("exported source")
    prefix = (
        (
            "env",
            f"GIT_DIR={parent / '.git'}",
            f"GIT_INDEX_FILE={parent / '.git/hook-index'}",
        )
        if prefixed
        else ()
    )
    ci_local.index_export(checkout, prefix=prefix)
    assert (checkout / ".git").is_dir(), (
        "must initialize the export, never the caller's repository"
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(parent), "config", "core.bare"], env=clean, text=True
        ).strip()
        == "false"
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(parent), "ls-files"], env=clean, text=True
        ).strip()
        == "keep.txt"
    )
    assert not (parent / ".git/hook-index").exists()
    assert (
        subprocess.check_output(
            ["git", "-C", str(checkout), "ls-files"], env=clean, text=True
        ).strip()
        == "exported.txt"
    )
