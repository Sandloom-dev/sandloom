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

from sandloom.core.cli import run_tests

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
# Fails before it reads or writes a file: the standard composition has no console
# module, so the build is proven by the refusal that names the missing declaration.
_ADMIN_EXPORT = (
    "from sandloom.core.cli.admin import main; "
    "raise SystemExit(main(['export', '--output', 'probe-manifest.json', '--check']))"
)
_CHECK_BOOT = (
    "from sandloom.core.cli.check import main; raise SystemExit(main(['--boot']))"
)
_MIGRATE_DRIFT = (
    "from sandloom.core.cli.migrate import main; raise SystemExit(main(['drift']))"
)


@pytest.mark.subprocess_probe
@pytest.mark.parametrize(
    ("code", "returncode", "ok_line"),
    [
        (_GENERATE, 0, "generated files up to date"),
        (_STATIC, 0, "schema & deploy: OK"),
        (_CONTRACTS, 0, "contract snapshots up to date"),
        (_ROUTES, 0, "route snapshots up to date"),
        (_ADMIN_EXPORT, 1, "no composed module declares a generated file"),
        (_CHECK_BOOT, 0, "boot clean"),
    ],
    ids=[
        "generated files",
        "schema & deploy",
        "contract snapshots",
        "route snapshots",
        "admin export",
        "check --boot",
    ],
)
def test_the_authoring_step_builds_the_standard_composition_by_default(
    tmp_path: Path, code: str, returncode: int, ok_line: str
) -> None:
    run = _child(code, tmp_path, composition_root=None)
    assert run.returncode == returncode, run.stdout + run.stderr
    assert ok_line in run.stdout + run.stderr
    assert "needs a 'driver'" not in run.stdout + run.stderr


@pytest.mark.subprocess_probe
def test_migrate_does_not_read_the_project_default(tmp_path: Path) -> None:
    """``sandloom migrate`` names the app the deploy runs, in the variable only:
    with the key present and the variable unset it builds the lean root and
    refuses the driverless INFRA, as it did before the key existed."""
    run = _child(_MIGRATE_DRIFT, tmp_path, composition_root=None)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "INFRA['cache']['default'] needs a 'driver'" in run.stdout + run.stderr


def _drift_step() -> run_tests.Step:
    steps = run_tests.plan(
        _ROOT,
        None,
        full=False,
        gates=True,
        workers="1",
        ci=False,
        xdist=False,
        shuffle_seed=None,
    )
    return next(step for step in steps if step.name == "migration drift")


def test_sandloom_test_hands_the_drift_step_the_root_the_other_steps_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """migrate reads the variable only, so the step sets it: the project default
    when nothing selects a root, and the selected one when something does."""
    from sandloom.core.composition import COMPOSITION_ROOT_ENV
    from sandloom.core.config.settings import settings

    step = _drift_step()
    assert step.resolve_env is not None
    monkeypatch.delenv(COMPOSITION_ROOT_ENV, raising=False)
    monkeypatch.setattr(settings, "sandloom_composition_root", None)
    assert step.resolve_env() == {COMPOSITION_ROOT_ENV: "sandloom_standard.main"}
    monkeypatch.setenv(COMPOSITION_ROOT_ENV, "probe.asgi")
    assert step.resolve_env() == {COMPOSITION_ROOT_ENV: "probe.asgi"}


@pytest.mark.subprocess_probe
def test_an_explicit_root_still_beats_the_project_default(tmp_path: Path) -> None:
    """Bite: were the project default to win over the env selection, the lean
    root named here would not be built and the driverless INFRA would pass."""
    run = _child(_GENERATE, tmp_path, composition_root="sandloom.main")
    assert run.returncode == 2, run.stdout + run.stderr
    assert "INFRA['cache']['default'] needs a 'driver'" in run.stdout
