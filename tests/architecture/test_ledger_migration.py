"""The ledger migration of the Public API 1.0 wave (spec §9.2, §10.3 item 1).

Compares ``guarantees.yml`` with its state before the wave
(``_fixtures/ledger_pre_api10.json``: fingerprints of the ledger at
``92db0f99``, since CI has no git history) and requires:

* every id whose statement or gap changed, or that lost a test, or that is
  new, to be assigned by the spec (§9.2 affected entries, §9.3 ADR-0009
  sections, §9.4 new entries) or by a hand-off's ledger delta
  (``tests/fixtures/specs/api10-ledger-delta-*.md``);
* every pre-wave id to be live or recorded under ``removed:`` with a reason,
  an approval and a superseding id;
* ADR-0009 and ADR-0010 to have no ``planned`` section left.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND
LEDGER = BACKEND / "guarantees.yml"
FIXTURE = Path(__file__).parent / "_fixtures" / "ledger_pre_api10.json"
SPEC = REPO / "tests" / "fixtures" / "specs" / "public-api-1.0-spec.md"
DELTAS = sorted((REPO / "tests" / "fixtures" / "specs").glob("api10-ledger-delta-*.md"))

_ID = re.compile(r"\b([GC]-[A-Z]+)-(\d{2})\b")
# a bare number continuing the last prefix: "G-TENANT-01, 02, 04", "08/09",
# a range "01..05" / "01<en dash>07" / "21..24"
_EN_DASH = "\u2013"
_CONT = re.compile(r"^\s*(?:,|/|\.\.|\u2013|-|and)\s*(\d{2})\b")
# a whole prefix, named without a number ("G-MSTORE, C-MSTORE")
_PREFIX = re.compile(r"\b([GC]-[A-Z]+)\b(?!-\d)")


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _h(text: str, n: int) -> str:
    # Compare with the pre-rename checkpoint without treating the brand or
    # package path change as a Public API 1.0 guarantee change.
    # Historical migration fingerprints compare the same content across a
    # recorded filesystem relocation. The assertions and dispositions stay intact.
    paths = json.loads(
        (REPO / "tests/fixtures/specs/layout-path-map.json").read_text()
    )["paths"]
    for current, original in sorted(paths.items(), key=lambda item: -len(item[0])):
        text = text.replace(current, original)
    text = text.replace("SANDLOOM", "AJA").replace("Sandloom", "AJA")
    text = text.replace("sandloom", "aja")
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def ids_named_in(text: str) -> set[str]:
    """Every ledger id ``text`` names, shorthand continuations and ranges
    expanded (``G-TENANT-01, 02``; ``G-MSTORE-08/09``; ``G-REVOKE-21..24``)."""
    found: set[str] = set()
    for match in _ID.finditer(text):
        prefix, number = match.group(1), int(match.group(2))
        found.add(f"{prefix}-{number:02d}")
        rest, last = text[match.end() :], number
        while True:
            cont = _CONT.match(rest)
            if cont is None:
                break
            value = int(cont.group(1))
            sep = cont.group(0).split(cont.group(1))[0].strip()
            if sep in ("..", _EN_DASH, "-") and value > last:
                found.update(f"{prefix}-{n:02d}" for n in range(last, value + 1))
            else:
                found.add(f"{prefix}-{value:02d}")
            last = value
            rest = rest[cont.end() :]
    return found


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _before() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = json.loads(
        FIXTURE.read_text(encoding="utf-8")
    )["entries"]
    return entries


def new_prefixes() -> set[str]:
    """The prefixes §9.4 creates whole (named without a number, e.g. the
    ``G-MSTORE, C-MSTORE`` row) that had no id before the wave: every id
    under them is new. A bare mention of an existing prefix blesses nothing."""
    spec = SPEC.read_text(encoding="utf-8")
    table = _section(spec, "### 9.4 ", "## 10. Acceptance")
    existing = {ident.rsplit("-", 1)[0] for ident in _before()}
    return set(_PREFIX.findall(table)) - existing


def assigned_ids() -> set[str]:
    spec = SPEC.read_text(encoding="utf-8")
    allowed = ids_named_in(_section(spec, "### 9.2 ", "## 10. Acceptance"))
    for delta in DELTAS:
        allowed |= ids_named_in(delta.read_text(encoding="utf-8"))
    return allowed


def _is_assigned(ident: str, allowed: set[str], prefixes: set[str]) -> bool:
    return ident in allowed or ident.rsplit("-", 1)[0] in prefixes


def _entries(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        e["id"]: e
        for section in ("guarantees", "capabilities")
        for e in ledger.get(section) or []
    }


def _ledger() -> dict[str, Any]:
    return yaml.safe_load(LEDGER.read_text(encoding="utf-8"))


def changed_ids() -> set[str]:
    before = _before()
    ledger = _ledger()
    now = _entries(ledger)
    changed = set(now) - set(before)
    for ident, old in before.items():
        entry = now.get(ident)
        if entry is None:
            changed.add(ident)
            continue
        statement = _norm(entry.get("statement")) + "\x00" + _norm(entry.get("gap"))
        tests = {_h(t, 12) for t in entry.get("tests") or []}
        if _h(statement, 16) != old["s"] or not set(old["t"]) <= tests:
            changed.add(ident)
    return changed


def test_the_shorthand_parser_expands_lists_and_ranges() -> None:
    assert ids_named_in("G-TENANT-01, 02, 04; G-RLS-12") == {
        "G-TENANT-01",
        "G-TENANT-02",
        "G-TENANT-04",
        "G-RLS-12",
    }
    assert ids_named_in("G-MSTORE-08/09 and G-REVOKE-21..24") == {
        "G-MSTORE-08",
        "G-MSTORE-09",
        "G-REVOKE-21",
        "G-REVOKE-22",
        "G-REVOKE-23",
        "G-REVOKE-24",
    }
    assert ids_named_in(f"C-MIGRATE-01{_EN_DASH}07") == {
        f"C-MIGRATE-0{n}" for n in range(1, 8)
    }


def test_every_id_the_wave_changed_is_assigned() -> None:
    allowed, prefixes = assigned_ids(), new_prefixes()
    unassigned = sorted(
        i for i in changed_ids() if not _is_assigned(i, allowed, prefixes)
    )
    assert not unassigned, (
        f"ledger ids changed by the Public API 1.0 wave but assigned nowhere: "
        f"{unassigned}. Add each to spec §9.2-§9.4 or to the hand-off's "
        "tests/fixtures/specs/api10-ledger-delta-*.md with its disposition"
    )


def test_bare_prefixes_are_only_the_new_ones() -> None:
    prefixes = new_prefixes()
    assert {"G-MSTORE", "C-MSTORE", "G-ROUTE", "G-UNIT", "G-SDK"} <= prefixes
    assert not prefixes & {"G-MODULE", "G-REVOKE", "C-TEST", "G-BILLING"}


def test_every_pre_wave_id_is_live_or_recorded_as_removed() -> None:
    before = _before()
    ledger = _ledger()
    live = set(_entries(ledger))
    removed = {r["id"]: r for r in ledger.get("removed") or []}
    for ident in sorted(set(before) - live):
        record = removed.get(ident)
        assert record is not None, f"{ident} disappeared without a removed: record"
        for field in ("reason", "approved_by", "replaced_by"):
            assert record.get(field), f"removed {ident} lacks {field}"


def test_adr_0009_and_0010_have_no_planned_section() -> None:
    ledger = _ledger()
    for adr in (
        "0009-route-policy-and-session-freshness.md",
        "0010-public-api-1.0.md",
    ):
        planned = [
            heading
            for heading, value in (ledger["adr_sections"].get(adr) or {}).items()
            if value == "planned"
        ]
        assert not planned, f"{adr}: sections still planned: {planned}"
        assert adr not in (ledger.get("planned_adrs") or {})
