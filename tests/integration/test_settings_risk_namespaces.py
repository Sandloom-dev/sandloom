"""Namespaced accepted risks across processes (ADR-0015 decision 4, KB-51;
scenarios P3 and P4 of alternatives-d §11, owner answers #27, #28).

Real processes, an empty working directory and an explicit environment, as in
``test_module_settings_fixture.py``. The deployment is one image: every
installed module is "known" (K1), the fixture module outside ``src/`` is
loaded by ``SANDLOOM_MODULES`` with its directory on ``PYTHONPATH``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.common import INVALID_CONFIGURATION
from sandloom.core.tests.test_boot_configuration_checks import (
    _DEV_BASE,
    _MODULE_BASE,
    COMPOSED_ENTRY_POINTS,
    _run,
)

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "settings_modules"


def _values(
    base: dict[str, Any], modules: list[str], *risks: str, **extra: Any
) -> dict[str, Any]:
    return {
        **base,
        "SANDLOOM_MODULES": modules,
        "SANDLOOM_ACCEPTED_RISKS": list(risks),
        "PYTHONPATH": str(FIXTURES),
        **extra,
    }


#: case -> (modules this process loads, accepted risk ids, reason)
REFUSALS: dict[str, tuple[list[str], list[str], str]] = {
    # (b) a loaded module declares no such risk
    "undeclared-id-of-a-loaded-module": (
        ["kb_inventory"],
        ["kb_inventory.nope"],
        "module kb_inventory declares no risk 'nope'",
    ),
    # (c) a namespace nobody in the deployment knows: a typo in the module
    "unknown-namespace": (
        ["kb_inventory"],
        ["biling.fake_psp_in_prod"],
        "'biling' is not a module installed, loaded or listed",
    ),
    # (d) the flat spelling of a module's id has no alias
    "flat-module-id": (
        ["kb_inventory"],
        ["oversell_in_prod"],
        "names unknown risk id(s) ['oversell_in_prod']",
    ),
}


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("entry", sorted(COMPOSED_ENTRY_POINTS))
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_bad_risk_id_refuses_in_every_composed_entry_point(
    case: str, entry: str, tmp_path: Path
) -> None:
    """P4 (b), (c), (d): the same refusal and exit status in the app, worker,
    beat, ``migrate`` and the composed operator CLIs."""
    modules, risks, reason = REFUSALS[case]
    proc = _run(
        COMPOSED_ENTRY_POINTS[entry], _values(_MODULE_BASE, modules, *risks), tmp_path
    )
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-2000:])
    if case != "flat-module-id":  # the kernel Settings' own validation error
        last = proc.stderr.strip().splitlines()[-1]
        assert last.startswith(INVALID_CONFIGURATION), proc.stderr[-2000:]
    assert reason in proc.stderr, proc.stderr[-2000:]


@pytest.mark.subprocess_probe
def test_the_id_of_an_installed_module_this_service_does_not_load_is_inert(
    tmp_path: Path,
) -> None:
    """P4 (a): the tenancy service of a split, one shared anchor: billing is
    installed (known) but not loaded here, so its id is accepted and listed
    as not applicable, never refused."""
    values = _values(_DEV_BASE, ["kb_inventory"], "billing.fake_psp_in_prod")
    app = [sys.executable, "-c", "import sandloom.main"]
    proc = _run(app, values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "ACCEPTED RISK: billing.fake_psp_in_prod" not in proc.stderr


@pytest.mark.subprocess_probe
def test_an_accepted_module_risk_starts_and_is_logged(tmp_path: Path) -> None:
    """P3: ``kb_inventory.oversell_in_prod`` accepted: the process starts and
    logs the risk by its namespaced id."""
    values = _values(
        _MODULE_BASE,
        ["kb_inventory"],
        "kb_inventory.oversell_in_prod",
        KB_INVENTORY_ALLOW_OVERSELL="true",
    )
    proc = _run(COMPOSED_ENTRY_POINTS["app"], values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "ACCEPTED RISK: kb_inventory.oversell_in_prod" in proc.stderr


@pytest.mark.subprocess_probe
def test_without_the_risk_the_module_check_still_refuses_in_prod(
    tmp_path: Path,
) -> None:
    values = _values(_MODULE_BASE, ["kb_inventory"], KB_INVENTORY_ALLOW_OVERSELL="true")
    proc = _run(COMPOSED_ENTRY_POINTS["app"], values, tmp_path)
    assert proc.returncode == 1
    assert "risk kb_inventory.oversell_in_prod" in proc.stderr
