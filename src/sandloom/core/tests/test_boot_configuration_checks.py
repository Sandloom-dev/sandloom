"""Checks moved out of ``Settings`` still refuse EVERY entry point.

The configuration layer imports nothing above it, so the validators that
need kernel code — asymmetric JWT key material, internal-RPC keys / bindings,
``PRODUCTION_PROFILE=multi_tenant_strict`` — run at boot
(:func:`sandloom.core.app_factory.enforce_configuration`). Each must still stop
the ASGI app, the Celery worker, ``sandloom-migrate`` and the ``sandloom`` CLI before
they serve or touch anything, with the reason in the message — exactly as a
``Settings`` load failure does. Real processes, an empty working directory
(no ``.env``) and an explicit environment.

Module refusals (identity without ``TWOFA_ENCRYPTION_KEY`` in prod, billing
on the implicit fake PSP in prod) are raised while the module registers, so
they reach every process that builds the composition — the app, the Celery
worker and beat, ``sandloom migrate`` and the composed operator CLIs — with
the same prefix and reason. ``sandloom config dump`` builds no composition
and is not forced to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.common import INVALID_CONFIGURATION
from sandloom.core.signing_keys import generate_private_key, public_pem
from sandloom.core.tests.test_production_profile import _BASE as _STRICT_BASE

pytestmark = pytest.mark.slow

_DEV_BASE: dict[str, Any] = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/boot_checks_test",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": "boot-checks-not-a-real-secret-0123456789",
    "EVENT_BUS_BACKEND": "memory",
}

#: moved validator -> (environment, reason that must be reported)
CASES: dict[str, tuple[dict[str, Any], str]] = {
    "jwt-key-material": (
        {
            **_DEV_BASE,
            "JWT_SECRET": "",
            "JWT_ALGORITHM": "EdDSA",
            "JWT_PUBLIC_KEYS": {"k": public_pem(generate_private_key("RS256"))},
        },
        "JWT_PUBLIC_KEYS['k']",
    ),
    "rpc-keys": (
        {
            **_DEV_BASE,
            "SERVICE_BINDINGS": {
                "tenancy.membership.v1": {
                    "base_url": "http://tenancy:2222",
                    "service_id": "tenancy",
                }
            },
        },
        "RPC_DH_PRIVATE_KEY",
    ),
    "strict-profile": (
        {
            **_STRICT_BASE,
            "TENANCY_MODE": "single",
            "SINGLE_TENANT_ID": "00000000-0000-0000-0000-000000000001",
        },
        "TENANCY_MODE must be 'multi'",
    ),
}

#: entry point -> the code its process runs first
ENTRY_POINTS = {
    "app": "import sandloom.main",
    "worker": "from sandloom.core.worker import get_worker_app; get_worker_app()",
    "migrate": ("from sandloom.core.cli.migrate import main; main(['databases'])"),
    "cli": "import sys; sys.argv = ['sandloom', 'config', 'dump']; import sandloom; sandloom.main()",
}


def _env(values: dict[str, Any]) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")
        if name in os.environ
    }
    for name, value in values.items():
        env[name] = value if isinstance(value, str) else json.dumps(value)
    return env


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
@pytest.mark.parametrize("case", sorted(CASES))
def test_moved_validator_refuses_every_entry_point(
    case: str, entry: str, tmp_path: Path
) -> None:
    values, reason = CASES[case]
    proc = subprocess.run(
        [sys.executable, "-c", ENTRY_POINTS[entry]],
        cwd=tmp_path,
        env=_env(values),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-2000:])
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(INVALID_CONFIGURATION), proc.stderr[-2000:]
    assert reason in last, last


_MODULE_BASE: dict[str, Any] = dict(_STRICT_BASE)

#: module refusal -> (environment, reason that must be reported). Each names
#: its module set, so the case holds whatever else is installed.
MODULE_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "identity-twofa-key": (
        {
            **_MODULE_BASE,
            "SANDLOOM_MODULES": ["sandloom_identity"],
        },
        "module sandloom_identity: TWOFA_ENCRYPTION_KEY is required with ENVIRONMENT=prod",
    ),
    "billing-implicit-fake-psp": (
        {
            **_MODULE_BASE,
            "SANDLOOM_MODULES": ["sandloom_billing"],
        },
        "module sandloom_billing: BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod",
    ),
}


def _cli(*argv: str) -> list[str]:
    """The ``sandloom`` command line with ``argv``, as its console script runs it."""
    code = f"import sys; sys.argv = ['sandloom', *{list(argv)!r}]; import sandloom; sandloom.main()"
    return [sys.executable, "-c", code]


#: entry point that builds the composition -> its command line
COMPOSED_ENTRY_POINTS: dict[str, list[str]] = {
    "app": [sys.executable, "-c", "import sandloom.main"],
    "worker": [sys.executable, "-m", "celery", "-A", "sandloom.core.worker", "worker"],
    "beat": [sys.executable, "-m", "celery", "-A", "sandloom.core.worker", "beat"],
    "migrate": _cli("migrate", "databases"),
    "cli-outbox": _cli("outbox", "list-dead"),
    "cli-streams": _cli("streams", "consumers"),
    "cli-projection": _cli("projection", "list"),
    "cli-sagas": _cli("sagas", "list"),
    "cli-backup": _cli("backup", "plan"),
}


def _run(
    argv: list[str], values: dict[str, Any], cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in ``cwd`` with only ``values`` (plus PATH/HOME) set."""
    return subprocess.run(
        argv,
        cwd=cwd,
        env=_env(values),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("entry", sorted(COMPOSED_ENTRY_POINTS))
@pytest.mark.parametrize("case", sorted(MODULE_CASES))
def test_module_refusal_stops_every_composed_entry_point(
    case: str, entry: str, tmp_path: Path
) -> None:
    """A module's production refusal stops every process that builds the
    composition, not only the API's lifespan: exit 1, the kernel prefix and
    the module's reason as the last line."""
    values, reason = MODULE_CASES[case]
    proc = _run(COMPOSED_ENTRY_POINTS[entry], values, tmp_path)
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-2000:])
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(INVALID_CONFIGURATION), proc.stderr[-2000:]
    assert reason in last, last


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("case", sorted(MODULE_CASES))
def test_config_dump_is_not_forced_to_build_the_composition(
    case: str, tmp_path: Path
) -> None:
    """``sandloom config dump`` reads settings only: a module refusal does not
    reach it (it builds no composition)."""
    values, _reason = MODULE_CASES[case]
    proc = _run(_cli("config", "dump"), values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert INVALID_CONFIGURATION not in proc.stderr


@pytest.mark.subprocess_probe
def test_module_cases_build_once_the_configuration_is_fixed(tmp_path: Path) -> None:
    """Control: the module-case environment builds the app once both module
    settings are fixed, so each case above fails on its own reason only."""
    values = {
        **_MODULE_BASE,
        "SANDLOOM_MODULES": ["sandloom_identity", "sandloom_billing"],
        "TWOFA_ENCRYPTION_KEY": "9f1c4e7a2b8d6035e1f94c7ab23d58e06f7b1a9c4d2e85f3071b6c9ae4d2f815",
        "SANDLOOM_ACCEPTED_RISKS": ["fake_psp_in_prod"],
    }
    proc = _run(
        [sys.executable, "-c", "import sandloom.main; print('built')"], values, tmp_path
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("built")
