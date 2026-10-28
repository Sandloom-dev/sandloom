"""M8: `sandloom test` — module tests, the parallel suite, the static gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli import run_tests


def _names(steps: list[run_tests.Step]) -> list[str]:
    return [s.name for s in steps]


def test_the_plan_of_the_framework_tree() -> None:
    root = Path(__file__).resolve().parents[4]  # backend/
    steps = run_tests.plan(root, "billing", full=False, gates=True, workers="auto")
    assert _names(steps) == [
        "generated files",
        "schema & deploy",
        "billing",
        "suite (not slow)",
        "ruff check",
        "ruff format",
        "mypy",
        "sandloom check",
        "architecture tests",
        "lint-imports",
        "contract snapshots",
        "route snapshots",
        "migration drift",
        "public API baseline",
    ]
    suite = steps[3]
    assert suite.argv[-6:] == ["-n", "auto", "--dist", "loadfile", "-m", "not slow"]
    assert suite.env["SANDLOOM_TEST_REDIS_DB_STRIDE"]
    assert steps[2].argv[-1].endswith("sandloom_billing/tests")
    full = run_tests.plan(root, None, full=True, gates=True, workers="auto")
    assert _names(full)[-1] == "typing tests"


def test_full_runs_the_slow_tests_and_no_gates_skips_them() -> None:
    root = Path(__file__).resolve().parents[4]
    steps = run_tests.plan(root, None, full=True, gates=False, workers="4")
    assert _names(steps) == ["full suite"]
    assert "not slow" not in steps[0].argv


def test_the_test_steps_run_in_a_seeded_random_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every test step loads the shuffle plugin with ONE seed; ``sandloom test``
    picks a fresh one unless ``--seed`` replays it or ``--no-shuffle``."""
    root = Path(__file__).resolve().parents[4]
    steps = run_tests.plan(
        root, "billing", full=True, gates=False, workers="2", shuffle_seed=42
    )
    order = ["-p", run_tests.SHUFFLE_PLUGIN, "--sandloom-shuffle=42"]
    for step in steps:  # the module's tests and the full suite
        argv = step.argv
        assert any(argv[i : i + 3] == order for i in range(len(argv))), argv
    plain = run_tests.plan(root, None, full=True, gates=False, workers="2")
    assert run_tests.SHUFFLE_PLUGIN not in plain[0].argv
    assert run_tests.main(["--plan", "--no-gates", "--seed", "7"]) == 0
    assert "--sandloom-shuffle=7" in capsys.readouterr().out
    assert run_tests.main(["--plan", "--no-gates", "--no-shuffle"]) == 0
    assert "--sandloom-shuffle" not in capsys.readouterr().out
    assert run_tests.main(["--plan", "--no-gates"]) == 0
    assert "--sandloom-shuffle=" in capsys.readouterr().out


def test_ci_never_writes_so_ruff_check_never_fixes() -> None:
    """A project's ``fix = true`` (every ``sandloom new`` app) would let ``ruff
    check`` fix a violation and pass; ``--ci`` writes nothing, so it fails."""
    root = Path(__file__).resolve().parents[4]
    for ci in (False, True):
        steps = run_tests.plan(root, None, full=False, gates=True, workers="2", ci=ci)
        ruff = next(s for s in steps if s.name == "ruff check")
        assert ("--no-fix" in ruff.argv) is ci


def test_sandloom_test_never_writes_a_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev and ``--ci`` alike, the ``generated files`` step only checks
    (``sandloom generate`` writes): its call passes ``--check``."""
    root = Path(__file__).resolve().parents[4]
    seen: list[list[str]] = []
    monkeypatch.setattr(run_tests, "_generated", lambda argv: seen.append(argv) or 0)
    for ci in (False, True):
        steps = run_tests.plan(root, None, full=False, gates=True, workers="2", ci=ci)
        step = next(s for s in steps if s.name == "generated files")
        assert step.call is not None and step.call() == 0
    assert seen == [["--root", str(root), "--check"]] * 2


def test_an_unknown_module_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nope"):
        run_tests.plan(tmp_path, "nope", full=False, gates=False, workers="auto")


def test_every_step_runs_and_the_summary_names_the_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    steps = [
        run_tests.Step("suite (not slow)", call=lambda: 0),
        run_tests.Step("route snapshots", call=lambda: 1),
        run_tests.Step("ruff check", call=lambda: 0),
    ]
    outcomes = run_tests.run(steps, Path.cwd())
    assert [o.code for o in outcomes] == [0, 1, 0]
    text = run_tests.summary(outcomes)
    assert "FAIL (exit 1)" in text and "sandloom explain SANDLOOM-T001" in text
    assert text.rstrip().endswith("sandloom test: FAILED (route snapshots)")


def test_every_xdist_step_gets_the_redis_stride_default() -> None:
    root = Path(__file__).resolve().parents[4]
    steps = run_tests.plan(root, "billing", full=True, gates=True, workers="auto")
    xdist = [s for s in steps if "-n" in s.argv]
    assert {s.name for s in xdist} == {"full suite", "architecture tests"}
    for step in xdist:
        assert step.env.get("SANDLOOM_TEST_REDIS_DB_STRIDE") == "auto", step.name


def test_an_exported_stride_wins_over_the_step_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setenv("SANDLOOM_TEST_REDIS_DB_STRIDE", "32")
    out = tmp_path / "out"
    step = run_tests.Step(
        "probe",
        [
            sys.executable,
            "-c",
            f"import os; open({str(out)!r}, 'w').write(os.environ['SANDLOOM_TEST_REDIS_DB_STRIDE'])",
        ],
        env={"SANDLOOM_TEST_REDIS_DB_STRIDE": "auto"},
    )
    assert run_tests.run([step], tmp_path)[0].code == 0
    assert out.read_text() == "32"


def test_the_test_env_is_dotenv_test_with_sandloom_test_overrides_on_top(
    tmp_path: Path,
) -> None:
    """Every step (in-process ones too) sees `.env.test`, then SANDLOOM_TEST_*: a
    checkout without `.env` still runs the snapshot checks."""
    import json
    import os
    import subprocess
    import sys

    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env.test").write_text(
        "BROKER_URL=redis://file:6379/1\nSANDLOOM_DXPROBE=from-file\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"BROKER_URL", "SANDLOOM_DXPROBE", "PYTEST_XDIST_WORKER"}
        and not k.startswith("SANDLOOM_TEST_")
    }
    env["SANDLOOM_TEST_BROKER_URL"] = "redis://override:6379/2"
    code = (
        "import json, os, pathlib\n"
        "from sandloom.core.cli.run_tests import load_project_test_env\n"
        f"load_project_test_env(pathlib.Path({str(project)!r}))\n"
        "print(json.dumps({k: os.environ.get(k) for k in ('BROKER_URL', 'SANDLOOM_DXPROBE')}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == {
        "BROKER_URL": "redis://override:6379/2",
        "SANDLOOM_DXPROBE": "from-file",
    }
    assert run_tests.load_project_test_env(tmp_path / "nowhere") is None
