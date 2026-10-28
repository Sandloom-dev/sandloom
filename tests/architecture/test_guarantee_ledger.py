"""The guarantee & capability ledger is a gate (``guarantees.yml``).

Every guarantee Sandloom promises and every capability a user relies on is an entry
with a stable id and the tests / ``sandloom check`` rules / import contracts that
prove it (``tools/checks/guarantees.py`` holds the checker). This test fails when:

- an entry's enforcing test (node id), rule or import contract no longer exists;
- an issued id is missing from the ledger without a ``removed:`` record that
  carries a reason and the owner's approval note (``approved_by``);
- an ADR (every ``docs/adr/NNNN-*.md``) has a ``##`` / ``###`` heading that
  ``adr_sections`` does not map to ledger ids (or mark ``informational``) — a
  new ADR, or a new section of one, needs its ledger entries in the same change;
- an entry has no enforcement and is not recorded as a ``gap``;
- ``docs/reference/guarantees.md`` is stale (regenerate with
  ``uv run python tools/checks/guarantees.py render``).

A change that removes or infers a declaration keeps this green and shows the
enforcing tests passing: ``uv run pytest -m guarantees src``.
"""

from __future__ import annotations

import copy
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "checks" / "guarantees.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sandloom_guarantee_ledger", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


ledger_mod = _load_module()


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    loaded: dict[str, Any] = ledger_mod.load().raw
    return loaded


def _problems(raw: dict[str, Any], adr_dir: Path | None = None) -> list[str]:
    kwargs = {} if adr_dir is None else {"adr_dir": adr_dir}
    problems: list[str] = ledger_mod.check(ledger_mod.parse(raw), **kwargs)
    return problems


def test_ledger_is_green() -> None:
    problems = ledger_mod.check()
    assert not problems, "guarantees.yml:\n  " + "\n  ".join(problems)


def test_reference_page_is_fresh() -> None:
    expected = ledger_mod.render()
    actual = ledger_mod.REFERENCE.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/reference/guarantees.md is stale — run "
        "`uv run python tools/checks/guarantees.py render`"
    )


def test_ledger_is_not_empty(raw: dict[str, Any]) -> None:
    assert len(raw["guarantees"]) >= 50
    assert len(raw["capabilities"]) >= 15


# --------------------------------------------------------------------------- #
# Positive detection: the gate catches each failure it promises to catch.


def test_detects_vanished_enforcing_test(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("tests"))
    entry["tests"] = [
        entry["tests"][0].split("::")[0] + "::test_does_not_exist_anywhere"
    ]
    assert any("enforcing test vanished" in p for p in _problems(mutated))


def test_detects_vanished_test_file(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = next(e for e in mutated["guarantees"] if e.get("tests"))
    entry["tests"] = ["tests/architecture/test_nope.py::test_x"]
    assert any("does not exist" in p for p in _problems(mutated))


def test_detects_unknown_rule(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    mutated["guarantees"][0]["rules"] = ["no-such-rule"]
    assert any("unknown sandloom check rule" in p for p in _problems(mutated))


def test_detects_deleted_entry_without_removed_record(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    victim = mutated["guarantees"].pop(0)
    problems = _problems(mutated)
    assert any(victim["id"] in p and "removed:" in p for p in problems), problems


def test_deleting_the_last_entry_of_a_prefix_is_caught(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    prefix = mutated["guarantees"][0]["id"].rsplit("-", 1)[0]
    last = max(
        (e for e in mutated["guarantees"] if e["id"].startswith(prefix + "-")),
        key=lambda e: int(e["id"].rsplit("-", 1)[1]),
    )
    mutated["guarantees"].remove(last)
    assert any(last["id"] in p for p in _problems(mutated))


def test_removed_record_needs_reason_and_owner_approval(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    victim = mutated["guarantees"].pop(0)
    mutated.setdefault("removed", [])
    mutated["removed"] = [*mutated["removed"], {"id": victim["id"], "statement": "x"}]
    problems = _problems(mutated)
    assert any("approved_by" in p for p in problems)
    assert any("'reason'" in p for p in problems)

    mutated["removed"][-1].update(
        reason="superseded by inference",
        approved_by="owner: ok in #123",
        date="2026-10-25",
    )
    assert not [p for p in _problems(mutated) if victim["id"] in p]


def test_entry_without_enforcement_must_be_a_gap(raw: dict[str, Any]) -> None:
    mutated = copy.deepcopy(raw)
    entry = mutated["guarantees"][0]
    for key in ("tests", "rules", "import_contracts", "gap", "gap_size"):
        entry.pop(key, None)
    assert any("record it as a gap" in p for p in _problems(mutated))


def test_new_adr_section_needs_an_entry(raw: dict[str, Any], tmp_path: Path) -> None:
    adr_dir = tmp_path / "adr"
    shutil.copytree(ledger_mod.ADR_DIR, adr_dir)
    existing = sorted(adr_dir.glob("0005-*.md"))[0]
    existing.write_text(
        existing.read_text(encoding="utf-8")
        + "\n### 9. A brand new promise\n\nText.\n",
        encoding="utf-8",
    )
    (adr_dir / "0099-new-decision.md").write_text(
        "# ADR-0099: New\n\n## Decision\n\nWe promise X.\n", encoding="utf-8"
    )
    problems = _problems(raw, adr_dir)
    assert any("9. A brand new promise" in p for p in problems), problems
    assert any("0099-new-decision.md is not mapped" in p for p in problems), problems


def test_headings_inside_code_fences_are_ignored(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    doc.write_text(
        "## Real\n\n```bash\n# not a heading\n## nor this\n```\n### Also real\n"
    )
    assert ledger_mod.headings(doc) == ["Real", "Also real"]


def test_marker_matching() -> None:
    ids = ("src/a/test_x.py::test_y", "src/a/test_z.py::TestK")
    assert ledger_mod.item_matches("src/a/test_x.py::test_y[param-1]", ids)
    assert ledger_mod.item_matches("src/a/test_z.py::TestK::test_m", ids)
    assert not ledger_mod.item_matches("src/a/test_x.py::test_y2", ids)
