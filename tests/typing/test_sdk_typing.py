"""The L1 typing laws under mypy ``--strict`` and pyright (spec §8.1, Appendix E).

``proofs/`` holds files that both checkers must pass. Expected refusals are
pinned with ``# type: ignore[...]``: both run with unused-ignore reporting,
so a law that stops holding (an error that disappears) fails as well as a
new error. Run by ``sandloom test --full`` (step "typing tests").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[2]
PROOFS = sorted(str(p) for p in (HERE / "proofs").glob("*.py"))

pytestmark = pytest.mark.slow


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=600
    )


def test_proofs_exist() -> None:
    assert PROOFS, "tests/typing/proofs is empty"


def test_mypy_strict_accepts_the_proofs() -> None:
    proc = _run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(HERE / "mypy.ini"),
            "--no-incremental",
            *PROOFS,
        ],
        BACKEND,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Success: no issues found" in proc.stdout


def test_pyright_strict_accepts_the_proofs() -> None:
    pyright = shutil.which("pyright", path=str(Path(sys.executable).parent))
    assert pyright is not None, "pyright is a pinned dev tool: run `uv sync`"
    proc = _run(
        [
            pyright,
            "--pythonpath",
            sys.executable,
            "-p",
            str(HERE / "pyrightconfig.json"),
        ],
        HERE,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 errors" in proc.stdout
