"""M8: `sandloom test` — module tests, the parallel suite, the static gates."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sandloom.core.archcheck.runner import Violation
from sandloom.core.cli import check, run_tests
from sandloom.core.testing import families
from sandloom.core.tests.test_testing_families import FakeFamily


def _names(steps: list[run_tests.Step]) -> list[str]:
    return [s.name for s in steps]


def test_full_runs_the_slow_tests_and_no_gates_skips_them() -> None:
    root = Path(__file__).resolve().parents[4]
    steps = run_tests.plan(root, None, full=True, gates=False, workers="4")
    assert _names(steps) == ["full suite"]
    assert "not slow" not in steps[0].argv


def test_ci_never_writes_so_ruff_check_never_fixes() -> None:
    """A project's ``fix = true`` (every ``sandloom new`` app) would let ``ruff
    check`` fix a violation and pass; ``sandloom test`` writes nothing, with or
    without ``--ci``, so the violation fails."""
    root = Path(__file__).resolve().parents[4]
    for ci in (False, True):
        steps = run_tests.plan(root, None, full=False, gates=True, workers="2", ci=ci)
        ruff = next(s for s in steps if s.name == "ruff check")
        assert "--no-fix" in ruff.argv, ci


def test_ruff_check_runs_with_the_gates_after_the_tests_in_both_modes() -> None:
    """Nothing fixes, so nothing needs to run first: the generated files, then
    the tests, then ``ruff check --no-fix`` with the other gates."""
    root = Path(__file__).resolve().parents[4]
    for ci in (False, True):
        names = _names(
            run_tests.plan(root, None, full=False, gates=True, workers="2", ci=ci)
        )
        assert names[:3] == ["generated files", "schema & deploy", "suite (not slow)"]
        assert names[3:5] == ["ruff check", "ruff format"]


def test_a_project_with_fix_true_fails_on_a_violation_and_stays_unchanged(
    tmp_path: Path,
) -> None:
    """The real ruff, on an app with ``fix = true`` and an unsorted import: the
    step fails and the source is as it was (``sandloom test`` writes nothing)."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nfix = true\nline-length = 60\n[tool.ruff.lint]\nselect = ["I"]\n'
    )
    api = tmp_path / "api.py"
    api.write_text("import sys\nimport os\n\nprint(os, sys)\n")
    before = api.read_text()
    steps = run_tests.plan(tmp_path, None, full=False, gates=True, workers="2")
    ruff = next(s for s in steps if s.name == "ruff check")
    assert run_tests._run_command(ruff, tmp_path) != 0
    assert api.read_text() == before


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


def test_the_parallel_steps_get_the_environment_every_family_asks_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel names no backend variable: an xdist step carries what each
    harness family's ``parallel_env`` says, and a serial one carries none of it."""
    monkeypatch.setattr(families, "_loaded", (FakeFamily(),))
    parallel = run_tests.plan(
        tmp_path, None, full=False, gates=False, workers="2", xdist=True
    )
    assert parallel[-1].env["FAKE_PARALLEL"] == "1"
    serial = run_tests.plan(
        tmp_path, None, full=False, gates=False, workers="2", xdist=False
    )
    assert "FAKE_PARALLEL" not in serial[-1].env
    monkeypatch.setattr(families, "_loaded", ())
    bare = run_tests.plan(
        tmp_path, None, full=False, gates=False, workers="2", xdist=True
    )
    assert "FAKE_PARALLEL" not in bare[-1].env


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


def test_a_command_step_reads_the_composition_root_the_project_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child of a ``resolve_env`` step (the migrate drift one) carries the
    root `_composition_env` resolved (the process environment does not hold it)."""
    monkeypatch.delenv(run_tests.COMPOSITION_ROOT_ENV, raising=False)
    monkeypatch.setattr(
        run_tests, "composition_root_name", lambda project_root: "acme.asgi"
    )
    out = tmp_path / "out"
    step = run_tests.Step(
        "probe",
        [
            sys.executable,
            "-c",
            f"import os; open({str(out)!r}, 'w').write(os.environ['SANDLOOM_COMPOSITION_ROOT'])",
        ],
        resolve_env=lambda: run_tests._composition_env(tmp_path),
    )
    assert run_tests._run_command(step, tmp_path) == 0
    assert out.read_text() == "acme.asgi"


@pytest.mark.parametrize(
    "failure",
    [
        run_tests.CompositionRootError("composition-root is not a dotted path"),
        SystemExit("settings refuse to load"),
    ],
)
def test_a_step_whose_environment_cannot_be_resolved_fails_with_its_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: BaseException
) -> None:
    """A malformed composition root or settings that exit is that one step's
    failure (message on stderr, exit 1), not a traceback that ends the run."""

    def refuse() -> dict[str, str]:
        raise failure

    step = run_tests.Step("probe", [sys.executable, "-c", "pass"], resolve_env=refuse)
    assert run_tests._run_command(step, tmp_path) == 1
    err = capsys.readouterr().err
    assert "sandloom test: probe:" in err and str(failure) in err


def test_an_unexpected_error_resolving_a_step_environment_still_raises(
    tmp_path: Path,
) -> None:
    """Only the two named refusals are a step failure: a bug stays loud."""

    def broken() -> dict[str, str]:
        raise ZeroDivisionError

    step = run_tests.Step("probe", [sys.executable, "-c", "pass"], resolve_env=broken)
    with pytest.raises(ZeroDivisionError):
        run_tests._run_command(step, tmp_path)


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
    infra = {"taskqueue": {"default": {"driver": "celery", "broker": "redis://file/1"}}}
    (project / ".env.test").write_text(
        f"INFRA='{json.dumps(infra)}'\nSANDLOOM_DXPROBE=from-file\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"INFRA", "SANDLOOM_DXPROBE", "PYTEST_XDIST_WORKER"}
        and not k.startswith("SANDLOOM_TEST_")
    }
    env["SANDLOOM_TEST_BROKER_URL"] = "redis://override:6379/2"
    code = (
        "import json, os, pathlib\n"
        "from sandloom.core.cli.run_tests import load_project_test_env\n"
        f"load_project_test_env(pathlib.Path({str(project)!r}))\n"
        "broker = json.loads(os.environ['INFRA'])['taskqueue']['default']['broker']\n"
        "print(json.dumps({'broker': broker, 'probe': os.environ.get('SANDLOOM_DXPROBE')}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    # the override replaced the broker INFRA declares; the flat name is not set
    assert loaded == {"broker": "redis://override:6379/2", "probe": "from-file"}
    assert run_tests.load_project_test_env(tmp_path / "nowhere") is None


def test_the_architecture_rules_run_once_per_sandloom_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `sandloom check` step records its violations where the run's later
    steps find them; the architecture tests read them back instead of running
    every rule again (only for the same manifest)."""
    seen: dict[str, object] = {}

    def fake_main(argv: list[str], *, record: Path | None = None) -> int:
        seen["record"] = record
        assert record is not None
        record.write_text("{}")
        return 0

    monkeypatch.setattr(check, "main", fake_main)
    reads = (
        "import os, sys; from pathlib import Path; "
        f"sys.exit(0 if Path(os.environ['{run_tests.ARCH_RECORD_ENV}']).is_file() else 3)"
    )
    steps = [
        run_tests.Step("sandloom check", call=lambda: run_tests._sandloom_check([])),
        run_tests.Step("architecture tests", [sys.executable, "-c", reads]),
    ]
    # an exported (stale) path never replaces the run's own file
    monkeypatch.setenv(run_tests.ARCH_RECORD_ENV, str(tmp_path / "stale.json"))
    outcomes = run_tests._run_recording_the_rules(steps, tmp_path)
    assert [o.code for o in outcomes] == [0, 0]  # the later step found the file
    assert isinstance(seen["record"], Path)
    assert run_tests._ARCH_RECORD.path is None  # nothing outlives the run

    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text("root: .\n")
    found = [
        Violation("module-boundaries", tmp_path / "a.py", "Line 3: imports b"),
        Violation("untagged-sensitive", tmp_path / "b.py", "x", severity="warning"),
    ]
    check._record(tmp_path / "rec.json", manifest, found)
    assert check.recorded_violations(tmp_path / "rec.json", manifest) == found
    other = tmp_path / "other" / "architecture_rules.yml"
    assert check.recorded_violations(tmp_path / "rec.json", other) is None
    assert check.recorded_violations(tmp_path / "missing.json", manifest) is None
    (tmp_path / "bad.json").write_text("{not json")
    assert check.recorded_violations(tmp_path / "bad.json", manifest) is None
