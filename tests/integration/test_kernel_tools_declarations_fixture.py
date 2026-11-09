"""The kernel's tools read a module's settings declaration (ADR-0015, KB-55;
scenarios P11 and P14 of design/kernel-boundary/_archive/alternatives-d-settings.md
§11).

The fixture module ``kb_inventory`` (``tests/fixtures/settings_modules``) lives
outside ``src/`` and declares ``console``, ``shared``, ``backup``, ``deploy``
and ``dev_env``: the deploy scaffold, the deploy-profile check (T014), the dev
env, the backup plan and ``config dump`` pick them up with no kernel edit, and
no kernel tool names a module's setting. ``config dump`` runs as a real process
(an empty working directory and an explicit environment, the harness of
``test_boot_configuration_checks.py``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import scaffold_deploy
from sandloom.core.config import module_settings as ms
from sandloom.core.testing import deploy_profile
from sandloom.core.tests.test_boot_configuration_checks import (
    _DEV_BASE,
    COMPOSED_ENTRY_POINTS,
    _cli,
    _run,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "settings_modules"

pytestmark = pytest.mark.slow


@pytest.fixture
def fixture_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``kb_inventory`` importable for the block, forgotten after it."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    for name in [n for n in sys.modules if n.split(".")[0] == "kb_inventory"]:
        monkeypatch.delitem(sys.modules, name)
    yield
    for name in [n for n in sys.modules if n.split(".")[0] == "kb_inventory"]:
        del sys.modules[name]


# ------------------------------------------------------------------- P14 ----

#: Names of the bundled modules' settings: no kernel tool spells one.
_MODULE_NAMES = (
    "BILLING_",
    "STRIPE_",
    "TWOFA_ENCRYPTION_KEY",
    "billing_provider",
    "refresh_token_expire_days",
)
_TOOLS = (
    "src/sandloom/core/testing/deploy_profile.py",
    "src/sandloom/core/cli/scaffold_deploy.py",
    "src/sandloom/core/migrations/backup.py",
    "src/sandloom/core/admin/api/platform.py",
    "src/sandloom/core/config/tiers.py",
    "src/sandloom/core/cli/doctor.py",
    "src/sandloom/core/cli/config.py",
    "src/sandloom/core/app_factory/ops.py",
)


@pytest.mark.parametrize("tool", _TOOLS)
def test_no_kernel_tool_names_a_module_setting(tool: str) -> None:
    """B-C7, B-C8, B-C9, B-N8 (I-03): tiers, diagnostics, doctor, the backup
    plan, the console list, T014, the dev env and the deploy scaffold read the
    modules' declarations; none names a bundled module's setting."""
    text = (REPO / tool).read_text(encoding="utf-8")
    assert [name for name in _MODULE_NAMES if name in text] == []


@pytest.mark.usefixtures("fixture_module")
def test_scaffold_env_comes_from_the_declaration() -> None:
    """B-N8: ``sandloom add deploy`` writes a module's env from its ``deploy``
    declaration, by module name or import path; a module that declares none
    (or is no package) needs none."""
    lines = scaffold_deploy.module_env("kb_inventory")
    model = ms.declared_model("kb_inventory")
    assert model is not None and lines == ms.declared_deploy(model)
    assert "KB_INVENTORY_WAREHOUSE: ${KB_INVENTORY_WAREHOUSE:-main}" in lines
    assert scaffold_deploy.module_env("kb_inventory_dup") == ()
    assert scaffold_deploy.module_env("kb_no_such_package") == ()


def _proc(**env: str) -> dict[str, Any]:
    return {"environment": env}


@pytest.mark.usefixtures("fixture_module")
def test_t014_holds_every_process_that_loads_the_module_to_its_shared_values() -> None:
    """D-B2, P9: a value a module declares shared must be equal in every
    process that loads it; a process without the module neither holds nor is
    held to the value."""
    modules = json.dumps(["kb_inventory"])
    common = {"SANDLOOM_MODULES": modules}
    problems: list[str] = []
    procs = {
        "api": _proc(**common, KB_INVENTORY_WAREHOUSE="eu"),
        "worker": _proc(**common, KB_INVENTORY_WAREHOUSE="us"),
        "other": _proc(SANDLOOM_MODULES=json.dumps([])),
    }
    deploy_profile._shared_settings(procs, problems.append)
    differs = [p for p in problems if "KB_INVENTORY_WAREHOUSE differs" in p]
    assert len(differs) == 1 and "kb_inventory" in differs[0]
    assert not [p for p in problems if "other" in p and "KB_INVENTORY" in p]

    problems.clear()
    procs["worker"] = _proc(**common)
    deploy_profile._shared_settings(procs, problems.append)
    assert [
        p for p in problems if "KB_INVENTORY_WAREHOUSE is missing in ['worker']" in p
    ]

    problems.clear()
    procs["worker"] = _proc(**common, KB_INVENTORY_WAREHOUSE="eu")
    deploy_profile._shared_settings(procs, problems.append)
    assert not [p for p in problems if "KB_INVENTORY" in p]


_COMPOSE = """\
services:
  inventory:
    environment:
      SANDLOOM_MODULES: '["kb_inventory"]'
      KB_INVENTORY_API_TOKEN: ${KB_INVENTORY_API_TOKEN:?set it}
      JWT_SECRET: ${JWT_SECRET:?set it}
"""

_COMPOSE_WITHOUT = _COMPOSE.replace('["kb_inventory"]', "[]")


@pytest.mark.usefixtures("fixture_module")
def test_dev_env_values_and_risks_come_from_the_declaration() -> None:
    """B-C7, P14: the dev env of ``sandloom keys dev-env`` holds what the
    loaded modules declare (a fresh value each time) and the risks that stack
    accepts; a profile that does not load the module gets neither."""
    shell = deploy_profile.dev_shell(_COMPOSE)
    assert shell["KB_INVENTORY_API_TOKEN"].startswith("dev-")
    assert "kb_inventory.oversell_in_prod" in shell["SANDLOOM_ACCEPTED_RISKS"].split(
        ","
    )
    assert (
        deploy_profile.dev_shell(_COMPOSE)["KB_INVENTORY_API_TOKEN"]
        != (shell["KB_INVENTORY_API_TOKEN"])
    )
    bare = deploy_profile.dev_shell(_COMPOSE_WITHOUT)
    assert "KB_INVENTORY_API_TOKEN" not in bare
    assert "kb_inventory" not in bare["SANDLOOM_ACCEPTED_RISKS"]


# ------------------------------------------------------------------- P11 ----


def _dump(tmp_path: Path, *args: str, **env: str) -> Any:
    values = {
        **_DEV_BASE,
        "SANDLOOM_MODULES": ["kb_inventory"],
        "PYTHONPATH": str(FIXTURES),
        **env,
    }
    return _run(_cli("config", "dump", *args), values, tmp_path)


@pytest.mark.subprocess_probe
def test_dump_lists_module_values_with_provenance_and_redaction(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("KB_INVENTORY_WAREHOUSE=dotenv-wh\n")
    proc = _dump(
        tmp_path,
        "--json",
        KB_INVENTORY_API_TOKEN="tok-very-secret",
        KB_INVENTORY_RESERVE_TTL_SECONDS="42",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "tok-very-secret" not in proc.stdout
    rows = {
        row["name"]: row
        for row in json.loads(proc.stdout)
        if row.get("module") == "kb_inventory"
    }
    assert rows["KB_INVENTORY_API_TOKEN"]["value"] == "<redacted>"
    assert rows["KB_INVENTORY_API_TOKEN"]["secret"] is True
    assert rows["KB_INVENTORY_API_TOKEN"]["source"] == "env"
    assert rows["KB_INVENTORY_RESERVE_TTL_SECONDS"]["value"] == 42
    assert rows["KB_INVENTORY_WAREHOUSE"]["value"] == "dotenv-wh"
    assert rows["KB_INVENTORY_WAREHOUSE"]["source"] == "dotenv"
    assert rows["KB_INVENTORY_ALLOW_OVERSELL"]["source"] == "default"
    text = _dump(tmp_path, KB_INVENTORY_API_TOKEN="tok-very-secret")
    assert "# module kb_inventory" in text.stdout
    assert "tok-very-secret" not in text.stdout


@pytest.mark.subprocess_probe
def test_dump_diagnoses_an_invalid_module_value_and_exits_zero(tmp_path: Path) -> None:
    """A module's refusal is a diagnosed entry, not an exit: the other
    settings are still listed (collect mode)."""
    proc = _dump(tmp_path, KB_INVENTORY_RESERVE_TTL_SECONDS="0")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "# INVALID - " in proc.stdout
    assert "KB_INVENTORY_RESERVE_TTL_SECONDS" in proc.stdout
    assert "ENVIRONMENT" in proc.stdout  # the kernel's table is still there
    as_json = json.loads(
        _dump(tmp_path, "--json", KB_INVENTORY_RESERVE_TTL_SECONDS="0").stdout
    )
    (diagnosed,) = [row for row in as_json if row.get("source") == "invalid"]
    assert "KB_INVENTORY_RESERVE_TTL_SECONDS" in diagnosed["value"]
    assert diagnosed["module"] == "kb_inventory"
    # the same value stops every serving process (collect mode is off there)
    served = _run(
        COMPOSED_ENTRY_POINTS["app"],
        {
            **_DEV_BASE,
            "SANDLOOM_MODULES": ["kb_inventory"],
            "PYTHONPATH": str(FIXTURES),
            "KB_INVENTORY_RESERVE_TTL_SECONDS": "0",
        },
        tmp_path,
    )
    assert served.returncode == 1


_KERNEL_VIEW = """
import json, sys
from dataclasses import asdict
from sandloom.core.cli import config
entries = config.effective_config(config.settings)
sys.stdout.write(json.dumps([asdict(e) for e in entries], indent=2, default=str)
                 if "--json" in sys.argv else config._render_text(entries))
sys.stdout.write("\\n")
"""


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_kernel_only_is_the_kernel_dump_byte_for_byte(
    flags: tuple[str, ...], tmp_path: Path
) -> None:
    """Answer #31: ``--kernel-only`` prints what the dump printed before
    modules declared settings (the kernel's table, nothing else) and builds
    no composition."""
    env = {"KB_INVENTORY_API_TOKEN": "tok-x"}
    kernel_only = _dump(tmp_path, "--kernel-only", *flags, **env)
    assert kernel_only.returncode == 0, kernel_only.stderr[-2000:]
    expected = _run(
        [sys.executable, "-c", _KERNEL_VIEW, *flags],
        {
            **_DEV_BASE,
            "SANDLOOM_MODULES": ["kb_inventory"],
            "PYTHONPATH": str(FIXTURES),
            **env,
        },
        tmp_path,
    )
    assert expected.returncode == 0, expected.stderr[-2000:]
    assert kernel_only.stdout == expected.stdout
    assert "KB_INVENTORY_" not in kernel_only.stdout.upper()  # no module setting
    full = _dump(tmp_path, *flags, **env)
    if not flags:  # the default dump is the kernel's table, then the modules'
        assert full.stdout.startswith(kernel_only.stdout.rstrip("\n"))
        assert "# module kb_inventory" in full.stdout


_PROBE = {
    "SANDLOOM_MODULES": ["kb_collect_probe"],
    "PYTHONPATH": str(FIXTURES),
}


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("entry", sorted(COMPOSED_ENTRY_POINTS))
def test_collect_mode_is_off_in_every_serving_entry_point(
    entry: str, tmp_path: Path
) -> None:
    """D-1: the switch is a context variable only ``config dump`` and
    ``doctor`` set: every entry point that builds the composition sees it off."""
    out = tmp_path / "probe.txt"
    proc = _run(
        COMPOSED_ENTRY_POINTS[entry],
        {**_DEV_BASE, **_PROBE, "KB_PROBE_OUT": str(out)},
        tmp_path,
    )
    assert out.is_file(), proc.stderr[-2000:]
    assert set(out.read_text().split()) == {"off"}
    assert proc.returncode == 1  # the probe's refusal stopped it


@pytest.mark.subprocess_probe
def test_collect_mode_is_on_for_the_dump_and_never_read_from_the_environment(
    tmp_path: Path,
) -> None:
    out = tmp_path / "probe.txt"
    proc = _run(
        _cli("config", "dump"),
        {**_DEV_BASE, **_PROBE, "KB_PROBE_OUT": str(out)},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert set(out.read_text().split()) == {"on"}
    assert "kb_collect_probe: stop here" in proc.stdout
    # an environment variable cannot turn it on for a serving process
    out.unlink()
    served = _run(
        COMPOSED_ENTRY_POINTS["app"],
        {
            **_DEV_BASE,
            **_PROBE,
            "KB_PROBE_OUT": str(out),
            "SANDLOOM_COLLECT_MODE": "1",
            "SANDLOOM_COLLECT": "1",
        },
        tmp_path,
    )
    assert set(out.read_text().split()) == {"off"} and served.returncode == 1


def test_only_the_diagnosing_commands_turn_collect_mode_on() -> None:
    """The switch is set in two places: ``config dump`` and ``doctor``."""
    users = sorted(
        str(path.relative_to(REPO))
        for path in (REPO / "src").rglob("*.py")
        if "tests" not in path.parts
        and "collect_mode(" in path.read_text(encoding="utf-8")
        and path.name != "module_settings.py"
    )
    assert users == [
        "src/sandloom/core/cli/config.py",
        "src/sandloom/core/cli/doctor.py",
    ]


_DIAGNOSTICS = """
import json
from fastapi.testclient import TestClient
import sandloom.main as main

with TestClient(main.app) as client:
    body = client.get("/diagnostics/core").json()
print("RESULT " + json.dumps(body["configuration"]))
"""


def _configuration_block(tmp_path: Path, **env: Any) -> dict[str, Any]:
    values = {
        **_DEV_BASE,
        "SANDLOOM_MODULES": ["kb_inventory"],
        "PYTHONPATH": str(FIXTURES),
        "INTERNAL_ENDPOINTS_OPEN_IN_DEV": "true",
        "KB_INVENTORY_ALLOW_OVERSELL": "true",
        **env,
    }
    proc = _run([sys.executable, "-c", _DIAGNOSTICS], values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    block: dict[str, Any] = json.loads(proc.stdout.rsplit("RESULT ", 1)[1])
    return block


@pytest.mark.subprocess_probe
def test_diagnostics_core_shows_the_modules_overrides_risks_and_tiers(
    tmp_path: Path,
) -> None:
    """Backlog item folded into KB-55: ``/diagnostics/core`` listed the
    kernel's accepted risks only. A module's weaker semantic setting is an
    override, its accepted risk a row, a risk of an installed module that is not
    loaded here is listed as not applicable, and each module's tiers counted."""
    weaker = _configuration_block(tmp_path)
    assert "KB_INVENTORY_ALLOW_OVERSELL" in {o["setting"] for o in weaker["overrides"]}
    assert weaker["module_tiers"]["kb_inventory"] == {"semantic": 1, "ops": 3}

    accepted = _configuration_block(
        tmp_path,
        SANDLOOM_ACCEPTED_RISKS=[
            "kb_inventory.oversell_in_prod",
            "billing.fake_psp_in_prod",
        ],
    )
    rows = {row["risk"]: row for row in accepted["accepted_risks"]}
    assert rows["kb_inventory.oversell_in_prod"]["in_effect"] is False  # dev
    assert rows["billing.fake_psp_in_prod"]["applicable"] is False
    assert "KB_INVENTORY_ALLOW_OVERSELL" not in {
        o["setting"] for o in accepted["overrides"]
    }  # reported as the accepted risk, not as an override
