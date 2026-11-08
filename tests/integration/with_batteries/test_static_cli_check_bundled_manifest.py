"""The static CLIs work with NO environment, and ``sandloom check`` explains PyYAML.

``sandloom.core/__init__`` used to build ``settings`` eagerly, so ANY
``import sandloom.core.<submodule>`` demanded DATABASE_URL / JWT_SECRET & co. — the
module generator (then ``sandloom scaffold``, now ``sandloom add``) crashed in a
fresh shell. The re-exports are now lazy (PEP 562); these tests run the
console scripts in a subprocess with an EMPTY environment, from a directory
with no ``.env``.

Moved from ``sandloom/core/tests/test_static_cli_no_env.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(sys.executable).parent

_BACKEND = Path(__file__).resolve().parents[3]


def _run_empty_env(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env={}, capture_output=True, text=True, timeout=120
    )


@pytest.mark.slow
@pytest.mark.subprocess_probe
def test_sandloom_check_runs_against_a_manifest_with_empty_environment(
    tmp_path: Path,
) -> None:
    """A real ``sandloom check`` run (manifest + analyzers) needs no env either."""
    proc = _run_empty_env(
        [
            str(_BIN / "sandloom"),
            "check",
            "--rules",
            str(_BACKEND / "architecture_rules.yml"),
        ],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sandloom check: OK" in proc.stdout
