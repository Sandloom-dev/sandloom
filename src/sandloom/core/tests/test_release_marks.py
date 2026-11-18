"""Released marks (ADR-0017): the pure rules and the exported-schema check.

Runs on the no-Redis lane (``src/sandloom/core/tests``): nothing here touches
a backing service.
"""

from __future__ import annotations

import json
from pathlib import Path
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


def test_no_committed_file_has_no_baseline_to_compare_with() -> None:
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


def test_a_file_written_fresh_records_every_entry_as_released() -> None:
    """No file is committed (deleted, moved, never generated): there is
    nothing to tell a draft from a release, and a draft is never inferred from
    absence, so everything is recorded as released."""
    after = _entries("m.a.v1", "m.b.v1")
    assert marks.next_marks({}, {}, after, release=False, fresh=True) == {
        "m.a.v1": 1,
        "m.b.v1": 1,
    }
    # An existing file, same state: the entries are new, so drafts.
    assert marks.next_marks({}, {}, after, release=False, fresh=False) == {}


def test_a_named_draft_is_written_as_a_draft_even_in_a_fresh_file_or_a_release() -> (
    None
):
    after = _entries("m.a.v1", "m.b.v1")
    fresh = marks.next_marks(
        {}, {}, after, release=False, fresh=True, drafts={"m.b.v1"}
    )
    assert fresh == {"m.a.v1": 1}
    released = marks.next_marks({}, {}, after, release=True, drafts={"m.b.v1"})
    assert released == {"m.a.v1": 1}


def test_a_released_entry_is_not_made_a_draft_by_naming_it() -> None:
    before = _entries("m.a.v1", "m.b.v1")
    with pytest.raises(marks.ReleaseMarkError, match="m.a.v1 is released"):
        marks.next_marks(
            {"m.a.v1": 2}, before, before, release=False, drafts={"m.a.v1", "m.b.v1"}
        )


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


def _installed(
    tmp_path: Path, committed: str | None, schema: dict[str, Any], package_mark: int
) -> generated.Generated:
    """What the application exports for an installed producer's event."""
    path = tmp_path / "m.e.v1.json"
    if committed is not None:
        path.write_text(committed, encoding="utf-8")
    file = generated.Generated(path, "", "SANDLOOM-T005")
    generated._installed_event_file("m.e.v1", file, schema, package_mark)
    return file


def test_an_installed_producers_break_is_refused_whatever_the_package_marks(
    tmp_path: Path,
) -> None:
    """The events of an installed package count as published: the schema this
    application exported earlier is always compared, a draft export (mark 0)
    and a package that marks nothing included."""
    assert _installed(tmp_path, None, _NEW, 0).problems == []  # no export yet
    for committed in (_committed(0), _committed(1), _committed(), _committed(7)):
        for package_mark in (0, 1):
            problems = _installed(tmp_path, committed, _NEW, package_mark).problems
            assert problems and problems[0].startswith("m.e.v1: "), committed
    assert _installed(tmp_path, _committed(1), _OLD, 0).problems == []  # unchanged


def test_the_mark_exported_for_an_installed_event_never_goes_down(
    tmp_path: Path,
) -> None:
    def written(committed: str | None, package_mark: int) -> object:
        file = _installed(tmp_path, committed, _OLD, package_mark)
        return json.loads(file.content)[marks.EVENT_MARK]

    assert written(None, 0) == 1  # a draft package: still published here
    assert written(_committed(0), 0) == 1  # a draft export of an earlier version
    assert written(_committed(7), 1) == 7  # never below the committed mark
    assert written(_committed(3), 5) == 5  # the package moved up
    assert written(_committed(), 0) == 1  # legacy export: released at 1


def test_a_malformed_export_mark_is_a_problem_not_a_pass(tmp_path: Path) -> None:
    [problem] = _installed(tmp_path, _committed("x"), _NEW, 0).problems
    assert "m.e.v1.json" in problem


def _package_snapshot(tmp_path: Path, released: object) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "module.snapshot.json"
    doc = {"module": "pkg", "events": {"m.e.v1": _OLD}, "released": released}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path, {"module": "pkg", "events": {"m.e.v1": _OLD}}


def test_a_corrupt_mark_in_an_installed_packages_snapshot_is_reported(
    tmp_path: Path,
) -> None:
    path, doc = _package_snapshot(tmp_path, {"m.e.v1": "one"})
    snapshot, exported = generated._event_files(
        doc, path, tmp_path / "shared", owned=False, release=False
    )
    assert len(snapshot.problems) == 1 and snapshot.problems[0].startswith("pkg: ")
    assert not snapshot.stale, "the package's file is never rewritten"
    [file] = exported
    assert json.loads(file.content)[marks.EVENT_MARK] == 1


def test_an_installed_packages_draft_channel_is_exported_as_published(
    tmp_path: Path,
) -> None:
    """A package that ships ``released: {}`` does not make its events drafts
    for the application."""
    path, doc = _package_snapshot(tmp_path, {})
    snapshot, exported = generated._event_files(
        doc, path, tmp_path / "shared", owned=False, release=True
    )
    assert snapshot.problems == []
    [file] = exported
    assert json.loads(file.content)[marks.EVENT_MARK] == 1


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


def test_a_new_snapshot_file_records_every_contract_as_released() -> None:
    doc = {"format": 1, "module": "m", "contracts": _entries("m.a.v1")}
    marked, strict = contract_compat.marked_snapshot(doc, None)
    assert marked["released"] == {"m.a.v1": 1}
    assert strict == {}  # nothing committed to compare with yet
    # Named as a draft (what the creating commands pass), it is not.
    marked, _ = contract_compat.marked_snapshot(doc, None, drafts={"m.a.v1"})
    assert marked["released"] == {}


def test_a_contract_new_to_an_existing_snapshot_is_a_draft() -> None:
    doc = {"format": 1, "module": "m", "contracts": _entries("m.a.v1", "m.b.v1")}
    committed = json.dumps(
        {"contracts": _entries("m.a.v1"), "released": {"m.a.v1": 1}}
    )
    marked, _ = contract_compat.marked_snapshot(doc, committed)
    assert marked["released"] == {"m.a.v1": 1}


def test_naming_a_released_contract_as_a_draft_is_refused() -> None:
    doc = {"format": 1, "module": "m", "contracts": _entries("m.a.v1")}
    committed = json.dumps(
        {"contracts": _entries("m.a.v1"), "released": {"m.a.v1": 1}}
    )
    with pytest.raises(marks.ReleaseMarkError, match="released"):
        contract_compat.marked_snapshot(doc, committed, drafts={"m.a.v1"})
