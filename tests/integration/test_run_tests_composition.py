"""The authoring steps of ``sandloom test`` and ``sandloom generate`` build the
checkout's own composition with no ``SANDLOOM_COMPOSITION_ROOT`` set.

The kernel selects no driver; an INFRA that names only urls (this checkout's
``.env.test``) refuses on the lean ``sandloom.main``. The repository's
``[tool.sandloom] composition-root`` (``pyproject.toml``) names the standard
composition for the steps that build the application (the generated files, the
schema and deploy checks). The pytest suites never read it: ``conftest.py``
picks their root.

Lane: the integration suite (``tests/integration``, batteries installed).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _child(
    code: str, tmp_path: Path, *, composition_root: str | None
) -> subprocess.CompletedProcess[str]:
    """``code`` in a clean child process run from the checkout root: a driverless
    INFRA, no ``.env`` and ``composition_root`` (``None``: no selection at all)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT": "dev",
        "SANDLOOM_SERVICE_NAME": "authoring-composition",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "JWT_SECRET": "authoring-composition-test-secret-0123456789abcdef0123",
        "INFRA": json.dumps({"cache": {"default": {"url": "redis://127.0.0.1:1/0"}}}),
    }
    if composition_root is not None:
        env["SANDLOOM_COMPOSITION_ROOT"] = composition_root
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )


_GENERATE = (
    "from sandloom.core.cli.generated import main; raise SystemExit(main(['--check']))"
)
_STATIC = (
    "from sandloom.core.testing.static_checks import main; "
    "raise SystemExit(main(['--root', '.']))"
)
_CONTRACTS = (
    "from sandloom.core.cli.contracts import main; "
    "raise SystemExit(main(['snapshot', '--check']))"
)
_ROUTES = (
    "from sandloom.core.cli.routes import main; "
    "raise SystemExit(main(['snapshot', '--check']))"
)


@pytest.mark.subprocess_probe
@pytest.mark.parametrize(
    ("code", "ok_line"),
    [
        (_GENERATE, "generated files up to date"),
        (_STATIC, "schema & deploy: OK"),
        (_CONTRACTS, "contract snapshots up to date"),
        (_ROUTES, "route snapshots up to date"),
    ],
    ids=["generated files", "schema & deploy", "contract snapshots", "route snapshots"],
)
def test_the_authoring_step_builds_the_standard_composition_by_default(
    tmp_path: Path, code: str, ok_line: str
) -> None:
    run = _child(code, tmp_path, composition_root=None)
    assert run.returncode == 0, run.stdout + run.stderr
    assert ok_line in run.stdout
    assert "needs a 'driver'" not in run.stdout + run.stderr


@pytest.mark.subprocess_probe
def test_an_explicit_root_still_beats_the_project_default(tmp_path: Path) -> None:
    """Bite: were the project default to win over the env selection, the lean
    root named here would not be built and the driverless INFRA would pass."""
    run = _child(_GENERATE, tmp_path, composition_root="sandloom.main")
    assert run.returncode == 2, run.stdout + run.stderr
    assert "INFRA['cache']['default'] needs a 'driver'" in run.stdout
