"""The bundled modules' production refusals stop every composed entry point
(moved from the kernel's ``test_boot_configuration_checks.py``, KB-05c).

Identity without ``TWOFA_ENCRYPTION_KEY`` in prod or without ``JWT_SECRET``,
and billing on the implicit fake PSP in prod, are raised while the module
registers, so they reach every process that builds the composition: the
app, the Celery worker and beat, ``sandloom migrate`` and the composed
operator CLIs, with the kernel's prefix and the module's reason.
``sandloom config dump`` builds no composition and is not forced to. Real
processes, an empty working directory and an explicit environment; the
harness (entry points, environment) is the kernel's.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.common import INVALID_CONFIGURATION
from sandloom.core.signing_keys import generate_private_key, private_pem, public_pem
from sandloom.core.tests.test_boot_configuration_checks import (
    _DEV_BASE,
    _MODULE_BASE,
    COMPOSED_ENTRY_POINTS,
    _cli,
    _run,
)

pytestmark = pytest.mark.slow


_ISSUER_KEY = generate_private_key("EdDSA")


#: An asymmetric-JWT issuer (identity mints): JWT_SECRET is optional for the
#: kernel there, but identity keys its emailed tokens with it.
_ASYMMETRIC_ISSUER: dict[str, Any] = {
    **_DEV_BASE,
    "JWT_ALGORITHM": "EdDSA",
    "JWT_PRIVATE_KEY": private_pem(_ISSUER_KEY),
    "JWT_KID": "k1",
    "JWT_PUBLIC_KEYS": {"k1": public_pem(_ISSUER_KEY)},
}


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
    "identity-jwt-secret": (
        {
            **_ASYMMETRIC_ISSUER,
            "JWT_SECRET": "",
            "SANDLOOM_MODULES": ["sandloom_identity"],
        },
        "module sandloom_identity: identity requires JWT_SECRET",
    ),
    "billing-implicit-fake-psp": (
        {
            **_MODULE_BASE,
            "SANDLOOM_MODULES": ["sandloom_billing"],
        },
        "module sandloom_billing: BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod",
    ),
}


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
        "SANDLOOM_ACCEPTED_RISKS": ["fake_psp_in_prod", "log_notifier_in_prod"],
    }
    proc = _run(
        [sys.executable, "-c", "import sandloom_standard.main; print('built')"],
        values,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("built")


@pytest.mark.subprocess_probe
def test_asymmetric_issuer_with_jwt_secret_builds(tmp_path: Path) -> None:
    """Control for ``identity-jwt-secret``: the same asymmetric issuer builds
    once ``JWT_SECRET`` is set, so that case fails on its own reason only."""
    values = {**_ASYMMETRIC_ISSUER, "SANDLOOM_MODULES": ["sandloom_identity"]}
    proc = _run(
        [sys.executable, "-c", "import sandloom_standard.main; print('built')"],
        values,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("built")
