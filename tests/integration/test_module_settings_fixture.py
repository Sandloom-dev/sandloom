"""A module outside ``src/`` declares its settings and gets the kernel's
guarantees (ADR-0015, KB-50; scenarios P1, P2, P5, P7, P15 of
alternatives-d §11).

The fixture modules live in ``tests/fixtures/settings_modules`` and load by
``SANDLOOM_MODULES`` with that directory on ``PYTHONPATH``. Real processes,
an empty working directory and an explicit environment: the harness of
``test_boot_configuration_checks.py`` (G-CONFIG-07).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.common import INVALID_CONFIGURATION
from sandloom.core.tests.test_boot_configuration_checks import (
    _DEV_BASE,
    _MODULE_BASE,
    COMPOSED_ENTRY_POINTS,
    _cli,
    _run,
)

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "settings_modules"
AUDIT = REPO / "tools" / "checks" / "import_audit.py"


def _with_fixture(values: dict[str, Any]) -> dict[str, Any]:
    return {"SANDLOOM_MODULES": ["kb_inventory"], **values, "PYTHONPATH": str(FIXTURES)}


# --------------------------------------------------------------------- P1 ----


def test_no_kernel_file_names_the_fixture_module() -> None:
    """I-13: the fixture needed no kernel edit, so no kernel file names it."""
    named = [
        str(path.relative_to(REPO))
        for path in (REPO / "src" / "sandloom").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json", ".toml", ".md", ".tmpl", ".yml", ".txt"}
        and "kb_inventory" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert named == []


# ------------------------------------------------------------ P1, P7 ----

_READER = """
import json, pathlib
import pydantic_settings.sources.providers.dotenv as _ps_dotenv
calls = {"dotenv_values": 0, "listed": []}
_real_values = _ps_dotenv.dotenv_values
def _counting(*a, **k):
    calls["dotenv_values"] += 1
    return _real_values(*a, **k)
_ps_dotenv.dotenv_values = _counting
_real_iterdir = pathlib.Path.iterdir
def _iterdir(self):
    calls["listed"].append(str(self))
    return _real_iterdir(self)
pathlib.Path.iterdir = _iterdir

import sandloom.main as main
from fastapi.testclient import TestClient
from sandloom.core.config import module_settings as ms
from sandloom.core.config.settings import settings
from sandloom.sdk.module import settings_of
from kb_inventory.settings import InventorySettings

with TestClient(main.app) as client:
    route = client.get("/api/v1/kb_inventory/config").json()
snapshot = ms.process_snapshot()
token = settings_of(InventorySettings).api_token
print("RESULT " + json.dumps({
    "route": route,
    "token": token.get_secret_value() if token else None,
    "log_level": settings.log_level,
    "dotenv_values": calls["dotenv_values"],
    "dotenv_parses": snapshot.dotenv_parses,
    "listed_secrets": [p for p in calls["listed"] if p.endswith("secrets")],
    "max_lookups": max(snapshot.secret_lookups.values()),
}))
"""


@pytest.mark.subprocess_probe
def test_fixture_reads_env_over_dotenv_over_secrets_from_one_read(
    tmp_path: Path,
) -> None:
    """P7: each source supplies a value, env > ``.env`` > ``SECRETS_DIR``; one
    ``.env`` parse and at most one secrets lookup per declared alias, the
    directory never listed. P1: the route and ``setup`` read the settings."""
    (tmp_path / ".env").write_text(
        "KB_INVENTORY_RESERVE_TTL_SECONDS=8\n"
        "KB_INVENTORY_WAREHOUSE=dotenv-wh\n"
        "LOG_LEVEL=DEBUG\n"
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "kb_inventory_reserve_ttl_seconds").write_text("7\n")
    (secrets / "kb_inventory_warehouse").write_text("secrets-wh\n")
    (secrets / "kb_inventory_api_token").write_text("from-secrets\n")
    values = _with_fixture(
        {
            **_DEV_BASE,
            "KB_INVENTORY_RESERVE_TTL_SECONDS": "9",
            "SECRETS_DIR": str(secrets),
        }
    )
    proc = _run([sys.executable, "-c", _READER], values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(x for x in proc.stdout.splitlines() if x.startswith("RESULT "))
    result = json.loads(line.removeprefix("RESULT "))
    assert result["route"] == {
        "reserve_ttl_seconds": 9,
        "warehouse": "dotenv-wh",
        "has_api_token": True,
    }
    assert result["token"] == "from-secrets"
    assert result["log_level"] == "DEBUG"  # the kernel reads the same .env
    assert result["dotenv_values"] == 1
    assert result["dotenv_parses"] == 1
    assert result["listed_secrets"] == []
    assert result["max_lookups"] == 1
    assert "kb_inventory reserve ttl 9s" in proc.stderr  # the setup reader


@pytest.mark.subprocess_probe
def test_semantic_override_of_a_module_is_logged(tmp_path: Path) -> None:
    values = _with_fixture({**_DEV_BASE, "KB_INVENTORY_ALLOW_OVERSELL": "true"})
    proc = _run([sys.executable, "-c", "import sandloom.main"], values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert (
        "CONFIG OVERRIDE: KB_INVENTORY_ALLOW_OVERSELL=true weakens the dev "
        "profile value false"
    ) in proc.stderr


# --------------------------------------------------------------------- P2 ----

#: refusal -> (environment on top of the strict prod base, reason)
CASES: dict[str, tuple[dict[str, Any], str]] = {
    "invalid-value": (
        {"KB_INVENTORY_RESERVE_TTL_SECONDS": "0"},
        "module kb_inventory: KB_INVENTORY_RESERVE_TTL_SECONDS: Input should be "
        "greater than or equal to 1",
    ),
    "oversell-in-prod": (
        {"KB_INVENTORY_ALLOW_OVERSELL": "true"},
        "module kb_inventory: KB_INVENTORY_ALLOW_OVERSELL=true is not allowed "
        "with ENVIRONMENT=prod",
    ),
    "duplicate-env-name": (
        {"SANDLOOM_MODULES": ["kb_inventory", "kb_inventory_dup"]},
        "SANDLOOM-B036: KB_INVENTORY_RESERVE_TTL_SECONDS is declared by module "
        "kb_inventory and by module kb_inventory_dup",
    ),
    "removed-setting": (
        {"KB_INVENTORY_LEGACY_SYNC": "true"},
        "module kb_inventory: KB_INVENTORY_LEGACY_SYNC was removed in 0.2 and "
        "KB_INVENTORY_LEGACY_SYNC=true is no longer supported",
    ),
    "typo-in-prod": (
        {"KB_INVENTORY_RESERVE_TTL_SECOND": "5"},
        "Unknown setting KB_INVENTORY_RESERVE_TTL_SECOND; did you mean "
        "KB_INVENTORY_RESERVE_TTL_SECONDS",
    ),
}


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("entry", sorted(COMPOSED_ENTRY_POINTS))
@pytest.mark.parametrize("case", sorted(CASES))
def test_module_settings_refusal_stops_every_composed_entry_point(
    case: str, entry: str, tmp_path: Path
) -> None:
    """P2 (D-H1, D-H3, D-F1): the same refusal, prefix and exit status in
    the app, worker, beat, ``migrate`` and the composed operator CLIs."""
    extra, reason = CASES[case]
    values = _with_fixture({**_MODULE_BASE, **extra})
    proc = _run(COMPOSED_ENTRY_POINTS[entry], values, tmp_path)
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-2000:])
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(INVALID_CONFIGURATION), proc.stderr[-2000:]
    assert reason in last, last


@pytest.mark.subprocess_probe
def test_doctor_reports_the_module_settings_refusal(tmp_path: Path) -> None:
    extra, reason = CASES["invalid-value"]
    proc = _run(_cli("doctor"), _with_fixture({**_MODULE_BASE, **extra}), tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output[-2000:]
    assert "FAIL" in output and reason in output, output[-3000:]


# --------------------------------------------------------------------- P5 ----


@pytest.mark.subprocess_probe
def test_optional_module_with_an_invalid_value_refuses(tmp_path: Path) -> None:
    """P5 (D-F5, answer #29): listed, optional, not required, invalid: the
    boot refuses with the module settings error; valid, it loads."""
    base = _with_fixture({**_DEV_BASE, "MODULE_REQUIRED": []})
    app = [sys.executable, "-c", "import sandloom.main"]
    bad = _run(app, {**base, "KB_INVENTORY_RESERVE_TTL_SECONDS": "99999"}, tmp_path)
    assert bad.returncode == 1, bad.stderr[-2000:]
    assert bad.stderr.strip().splitlines()[-1] == (
        f"{INVALID_CONFIGURATION}module kb_inventory: "
        "KB_INVENTORY_RESERVE_TTL_SECONDS: Input should be less than or equal to 3600"
    )
    good = _run(app, {**base, "KB_INVENTORY_RESERVE_TTL_SECONDS": "60"}, tmp_path)
    assert good.returncode == 0, good.stderr[-2000:]


# -------------------------------------------------------------------- P15 ----


def _audit(module: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    # ``-I`` ignores PYTHONPATH: the fixture directory goes on sys.path here.
    code = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(FIXTURES)!r}); "
        f"sys.argv = ['import_audit', '--json', {module!r}]; "
        f"runpy.run_path({str(AUDIT)!r}, run_name='__main__')"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.subprocess_probe
def test_import_audit_passes_the_fixture_and_fails_an_import_time_read(
    tmp_path: Path,
) -> None:
    """P15 (D-F3): declaring settings does nothing at import; reading them
    at import raises, so the audit fails the module."""
    clean = _audit("kb_inventory", tmp_path)
    assert clean.returncode == 0, clean.stdout[-2000:] + clean.stderr[-2000:]
    report = json.loads(clean.stdout)
    assert report["violations"] == [] and report["errors"] == []
    bad = _audit("kb_inventory_bad_import", tmp_path)
    assert bad.returncode == 1, bad.stdout[-2000:]
    (error,) = json.loads(bad.stdout)["errors"]
    assert "SettingsNotBoundError" in error and "never at import" in error
