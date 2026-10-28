"""tools/ci/unless_ci_passed.sh: pre-push checks that mirror CI jobs are skipped
only when a FULL local CI run passed on the exact tree being pushed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "ci" / "unless_ci_passed.sh"

# CI tests a `git archive` export (no .git): nothing to resolve a tree from.
pytestmark = pytest.mark.skipif(
    subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD~1"],
        cwd=REPO,
        capture_output=True,
    ).returncode
    != 0,
    reason="needs a git checkout with history",
)


def _run(tmp_path: Path, *, ref: str | None, ignore: bool = False) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRE_COMMIT_")}
    env["SANDLOOM_CI_PASS_DIR"] = str(tmp_path)
    if ref is not None:
        env["PRE_COMMIT_TO_REF"] = ref
    if ignore:
        env["SANDLOOM_PREPUSH_IGNORE_CI_PASS"] = "1"
    out = subprocess.run(
        ["bash", str(SCRIPT), "echo", "RAN"],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def _tree(ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{ref}^{{tree}}"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_runs_without_a_marker(tmp_path: Path) -> None:
    assert "RAN" in _run(tmp_path, ref="HEAD")


def test_skips_when_the_pushed_tree_passed(tmp_path: Path) -> None:
    (tmp_path / _tree("HEAD")).write_text("run_id=x\n")
    out = _run(tmp_path, ref="HEAD")
    assert "RAN" not in out and "skipped" in out


def test_other_tree_marker_does_not_skip(tmp_path: Path) -> None:
    (tmp_path / _tree("HEAD~1")).write_text("run_id=x\n")
    if _tree("HEAD~1") == _tree("HEAD"):  # pragma: no cover - empty commit
        return
    assert "RAN" in _run(tmp_path, ref="HEAD")


def test_manual_run_and_override_still_run(tmp_path: Path) -> None:
    (tmp_path / _tree("HEAD")).write_text("run_id=x\n")
    assert "RAN" in _run(tmp_path, ref=None)
    assert "RAN" in _run(tmp_path, ref="HEAD", ignore=True)
