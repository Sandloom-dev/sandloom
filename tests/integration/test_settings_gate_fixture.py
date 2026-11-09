"""``sandloom check`` / ``sandloom test`` settings gate over the fixture modules
(ADR-0015 decision 7, KB-56; scenarios P6 and P9 of alternatives-d §11).

The fixture modules of ``tests/fixtures/settings_modules`` are copied to a
temporary source root with an ``architecture_rules.yml`` that lists them, so
the gate runs exactly as it does on an application: nothing is booted.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.cli import check
from sandloom.core.testing import settings_gate, static_checks

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "settings_modules"
MODULES = ("kb_inventory", "kb_inventory_dup", "kb_inventory_bad_import")


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A source root with the three fixture modules and their manifest."""
    root = tmp_path / "app"
    shutil.copytree(
        FIXTURES, root / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    (root / "architecture_rules.yml").write_text(
        "version: 1\nmodules_path: src\nmodules_package: ''\nsource_root: src\n",
        encoding="utf-8",
    )
    for name in MODULES:  # import the copies, not an earlier test's modules
        monkeypatch.delitem(sys.modules, name, raising=False)
    return root


def _gate(root: Path) -> list[str]:
    return static_checks.settings_gate_problems(root, [])


def test_p9_a_name_two_modules_declare_is_flagged_though_they_never_load_together(
    app: Path,
) -> None:
    problems = _gate(app)
    assert any(
        "KB_INVENTORY_RESERVE_TTL_SECONDS is declared by module kb_inventory and "
        "by module kb_inventory_dup" in p
        for p in problems
    ), problems


def test_p6_a_module_that_reads_its_settings_at_import_is_reported(app: Path) -> None:
    problems = _gate(app)
    assert any(
        p.startswith(
            "kb_inventory_bad_import: cannot be imported for the settings gate"
        )
        for p in problems
    ), problems


def test_a_module_that_declares_settings_needs_its_snapshot_record(app: Path) -> None:
    problems = _gate(app)
    assert any(
        "module kb_inventory: module.snapshot.json records none of its settings" in p
        for p in problems
    )
    # the record `sandloom generate` writes satisfies it; the fixtures' names
    # all carry the module prefix, so nothing is grandfathered
    model = settings_gate.declared_modules(["kb_inventory"])[0][0].model
    section = settings_gate.settings_section(model, "kb_inventory", None)
    assert section["KB_INVENTORY_ALLOW_OVERSELL"] == {"tier": "semantic"}
    assert section["KB_INVENTORY_API_TOKEN"] == {"tier": "ops"}
    assert not any("legacy" in entry for entry in section.values())
    (app / "src" / "kb_inventory" / "module.snapshot.json").write_text(
        json.dumps({"format": 1, "settings": section}), encoding="utf-8"
    )
    assert not any("module kb_inventory:" in p and "snapshot" in p for p in _gate(app))


def test_the_gate_reaches_sandloom_check(app: Path) -> None:
    rules = load_rules(app / "architecture_rules.yml")
    text = "\n".join(check.settings_gate_diagnostics(rules))
    assert "SANDLOOM-B036" in text and "SANDLOOM-T019" in text
