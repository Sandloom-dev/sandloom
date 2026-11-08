"""M8: `sandloom test` — module tests, the parallel suite, the static gates.

Moved from ``sandloom/core/tests/test_run_tests_cli.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli import run_tests


def _names(steps: list[run_tests.Step]) -> list[str]:
    return [s.name for s in steps]


def test_the_plan_of_the_framework_tree() -> None:
    root = Path(__file__).resolve().parents[3]  # backend/
    steps = run_tests.plan(root, "billing", full=False, gates=True, workers="auto")
    assert _names(steps) == [
        "ruff check",
        "generated files",
        "schema & deploy",
        "billing",
        "suite (not slow)",
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
    suite = steps[4]
    assert suite.argv[-6:] == ["-n", "auto", "--dist", "loadfile", "-m", "not slow"]
    assert suite.env["SANDLOOM_TEST_REDIS_DB_STRIDE"]
    assert steps[3].argv[-1].endswith("sandloom_billing/tests")
    full = run_tests.plan(root, None, full=True, gates=True, workers="auto")
    assert _names(full)[-1] == "typing tests"


def test_the_test_steps_run_in_a_seeded_random_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every test step loads the shuffle plugin with ONE seed; ``sandloom test``
    picks a fresh one unless ``--seed`` replays it or ``--no-shuffle``."""
    root = Path(__file__).resolve().parents[3]
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


def test_every_xdist_step_gets_the_redis_stride_default() -> None:
    root = Path(__file__).resolve().parents[3]
    steps = run_tests.plan(root, "billing", full=True, gates=True, workers="auto")
    xdist = [s for s in steps if "-n" in s.argv]
    assert {s.name for s in xdist} == {"full suite", "architecture tests"}
    for step in xdist:
        assert step.env.get("SANDLOOM_TEST_REDIS_DB_STRIDE") == "auto", step.name
