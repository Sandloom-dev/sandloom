"""Every distribution builds, layers and works installed alone (slow).

Runs ``tools/packaging/check_package_distributions.py`` (the CI ``packages``
job): sdists and wheels of ``sandloom``, ``sandloom-identity``,
``sandloom-tenancy`` and ``sandloom-billing``; the import layering across them
(lazy imports included); ``pip install sandloom`` alone in a clean venv runs
every ``sandloom`` subcommand's ``--help``, ``sandloom docs <name>`` and
``sandloom new``; ``sandloom[standard]`` composes the modules by entry point.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "tools" / "packaging" / "check_package_distributions.py"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.subprocess_probe,
    pytest.mark.skipif(
        not (REPO / "packages").is_dir() or shutil.which("uv") is None,
        reason="needs the source tree's packages/ and uv",
    ),
]


def test_every_distribution_installs_and_runs_alone() -> None:
    done = subprocess.run(
        [sys.executable, str(CHECK)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    output = done.stdout + done.stderr
    assert done.returncode == 0, output[-6000:]
    assert "Import layering OK across 5 distributions" in output
    assert "lean sandloom OK" in output
    assert "standard set OK" in output
    assert "All package distribution checks passed." in output
