"""Released marks (ADR-0017): the pure rules and the exported-schema check.

Runs on the no-Redis lane (``src/sandloom/core/tests``): nothing here touches
a backing service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandloom.core import contract_compat
from sandloom.core.cli import generated
from sandloom.core.descriptors import release_marks as marks


def _entries(*names: str, version: int = 1) -> dict[str, Any]:
    return {name: {"v": version} for name in names}


# --------------------------------------------------------------------------- #
# old_marks: what a committed document releases
# --------------------------------------------------------------------------- #


def test_no_committed_file_releases_nothing() -> None:
    assert marks.old_marks(None, {}, where="f") == {}


def test_a_file_with_no_key_is_legacy_and_releases_every_entry() -> None:
    document = {"contracts": _entries("m.a.v1", "m.b.v1")}
    found = marks.old_marks(document, document["contracts"], where="f")
    assert found == {"m.a.v1": 1, "m.b.v1": 1}


def test_a_file_with_the_key_releases_exactly_what_it_lists() -> None:
    document = {"contracts": _entries("m.a.v1", "m.b.v1"), "released": {"m.a.v1": 3}}
    assert marks.old_marks(document, document["contracts"], where="f") == {"m.a.v1": 3}
    empty = {"contracts": _entries("m.a.v1"), "released": {}}
    assert marks.old_marks(empty, empty["contracts"], where="f") == {}


@pytest.mark.parametrize(
    "bad",
    [
        "yes",
        ["m.a.v1"],
        {"m.a.v1": "1"},
        {"m.a.v1": True},
        {"m.a.v1": -1},
        {"m.a.v1": 0},
    ],
)
def test_a_mark_that_is_not_a_revision_is_refused_not_ignored(bad: object) -> None:
    document = {"contracts": _entries("m.a.v1"), "released": bad}
    with pytest.raises(marks.ReleaseMarkError, match="f"):
        marks.old_marks(document, document["contracts"], where="f")


# --------------------------------------------------------------------------- #
# next_marks and strict_entries
# --------------------------------------------------------------------------- #


def test_a_draft_stays_unmarked_without_a_release_and_gets_revision_one_with_it() -> (
    None
):
    after = _entries("m.a.v1")
    assert marks.next_marks({}, {}, after, release=False) == {}
    assert marks.next_marks({}, {}, after, release=True) == {"m.a.v1": 1}


def test_a_released_entry_moves_one_revision_per_change_only() -> None:
    before = _entries("m.a.v1", "m.b.v1")
    after = {**before, "m.a.v1": {"v": 2}}
    found = marks.next_marks({"m.a.v1": 4, "m.b.v1": 2}, before, after, release=False)
    assert found == {"m.a.v1": 5, "m.b.v1": 2}


def test_a_removed_entry_loses_its_mark_and_a_release_keeps_the_others() -> None:
    before = _entries("m.a.v1", "m.b.v1")
    found = marks.next_marks(
        {"m.a.v1": 2, "m.b.v1": 2}, before, _entries("m.b.v1"), release=True
    )
    assert found == {"m.b.v1": 2}


def test_only_released_entries_are_compared() -> None:
    before = _entries("m.a.v1", "m.b.v1")
    assert marks.strict_entries({"m.a.v1": 1}, before) == _entries("m.a.v1")
    assert marks.strict_entries({}, before) == {}


# --------------------------------------------------------------------------- #
# The exported event schema
# --------------------------------------------------------------------------- #


def test_the_exported_mark_distinguishes_no_file_draft_released_and_legacy() -> None:
    assert marks.exported_mark(None, where="f") is None
    assert marks.exported_mark({"title": "E"}, where="f") == 1  # legacy
    assert marks.exported_mark({marks.EVENT_MARK: 0}, where="f") == 0
    assert marks.exported_mark({marks.EVENT_MARK: 5}, where="f") == 5
    with pytest.raises(marks.ReleaseMarkError):
        marks.exported_mark({marks.EVENT_MARK: "x"}, where="f")


def test_with_mark_and_without_mark_round_trip() -> None:
    schema = {"title": "E", "properties": {}}
    marked = marks.with_mark(schema, 2)
    assert marked[marks.EVENT_MARK] == 2
    assert marks.without_mark(marked) == schema
    assert marks.with_mark(marked, 0)[marks.EVENT_MARK] == 0


def test_labels() -> None:
    assert marks.label(None) == "unknown"
    assert marks.label(0) == "draft"
    assert marks.label(3) == "released, revision 3"


_OLD = {
    "title": "E",
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
}
_NEW = {"title": "E", "type": "object", "properties": {}, "required": []}


def _committed(mark: object = None) -> str:
    schema = dict(_OLD) if mark is None else {**_OLD, marks.EVENT_MARK: mark}
    return json.dumps(schema)


def test_an_installed_producers_break_is_refused_only_against_a_released_export() -> (
    None
):
    check = generated._exported_problems
    assert check("m.e.v1", None, _NEW) == []  # no export yet
    assert check("m.e.v1", _committed(0), _NEW) == []  # a draft
    for released in (_committed(1), _committed(), _committed(7)):  # 7, legacy, 1
        problems = check("m.e.v1", released, _NEW)
        assert problems and problems[0].startswith("m.e.v1: ")
    assert check("m.e.v1", _committed(1), _OLD) == []  # unchanged


def test_a_malformed_export_mark_is_a_problem_not_a_pass() -> None:
    [problem] = generated._exported_problems("m.e.v1", _committed("x"), _NEW)
    assert "m.e.v1.json" in problem


# --------------------------------------------------------------------------- #
# The contract snapshot document
# --------------------------------------------------------------------------- #


def test_marked_snapshot_keeps_the_marks_and_compares_only_released_contracts() -> None:
    doc = {
        "format": 1,
        "module": "m",
        "contracts": _entries("m.a.v1", "m.b.v1", version=2),
    }
    committed = json.dumps(
        {
            "contracts": _entries("m.a.v1", "m.b.v1"),
            "released": {"m.a.v1": 1},
        }
    )
    marked, strict = contract_compat.marked_snapshot(doc, committed)
    assert marked["released"] == {"m.a.v1": 2}  # a changed, released: one up
    assert strict == _entries("m.a.v1")  # m.b.v1 is a draft: not compared
    released, _ = contract_compat.marked_snapshot(doc, committed, release=True)
    assert released["released"] == {"m.a.v1": 2, "m.b.v1": 1}


def test_a_new_snapshot_file_is_all_drafts() -> None:
    doc = {"format": 1, "module": "m", "contracts": _entries("m.a.v1")}
    marked, strict = contract_compat.marked_snapshot(doc, None)
    assert marked["released"] == {} and strict == {}
