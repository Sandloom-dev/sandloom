"""Released marks of contracts and events (ADR-0017). Pure: no file, no import
of a module, no git.

A snapshot file records which of its entries are *released* with a revision.
``contracts.snapshot.json`` and ``module.snapshot.json`` hold a top-level
``released`` map (``{name: revision}``); an application's exported event file
holds ``x-sandloom-released`` (the revision of its one channel, ``0`` a draft).

- an entry with no mark is a **draft**: rewritten freely, no T004 / T005;
- a **released** entry keeps the strict rule; a compatible change of it moves
  its revision by one (computed here, never typed);
- a file written before the marks existed has no key at all: **legacy**, its
  entries count as released at revision 1, so an upgrade loosens nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The key of the ``{name: revision}`` map in a module's snapshot files.
RELEASED = "released"
#: The key of an exported event schema's revision (``0``: a draft).
EVENT_MARK = "x-sandloom-released"
#: The revision a release writes.
FIRST_REVISION = 1


class ReleaseMarkError(ValueError):
    """A mark in a committed file is not a revision."""


def _revision(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReleaseMarkError(
            f"{where}: a revision is a whole number of 1 or more, got {value!r}"
        )
    return value


def old_marks(
    document: Mapping[str, Any] | None, entries: Mapping[str, Any], *, where: str
) -> dict[str, int]:
    """The released names of a committed snapshot ``document`` (``None``: no
    file yet, so nothing is released). ``entries`` are the names the document
    lists: a document with no ``released`` key is legacy and releases them all
    at revision 1. An entry the map lists with ``0`` is a draft."""
    if document is None:
        return {}
    if RELEASED not in document:
        return dict.fromkeys(entries, FIRST_REVISION)
    found = document[RELEASED]
    if not isinstance(found, dict):
        raise ReleaseMarkError(f"{where}: 'released' must be an object of revisions")
    marks = {str(n): _revision(r, f"{where}: {n}") for n, r in found.items()}
    zero = [name for name, revision in marks.items() if revision == 0]
    if zero:
        raise ReleaseMarkError(
            f"{where}: {', '.join(sorted(zero))} marked released at revision 0; a "
            "draft has no entry in 'released'"
        )
    return marks


def next_marks(
    old: Mapping[str, int],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    release: bool,
) -> dict[str, int]:
    """The marks to write: a released name keeps its mark, one revision up
    when its entry changed (a compatible change: the caller refused a breaking
    one already); a draft becomes released when ``release``. A name that is
    gone loses its mark."""
    marks: dict[str, int] = {}
    for name, entry in after.items():
        revision = old.get(name)
        if revision is None:
            if release:
                marks[name] = FIRST_REVISION
            continue
        marks[name] = revision + (0 if before.get(name) == entry else 1)
    return dict(sorted(marks.items()))


def strict_entries(old: Mapping[str, int], before: Mapping[str, Any]) -> dict[str, Any]:
    """The committed entries a new document is compared with: the released
    ones only; a draft is not in the baseline."""
    return {name: entry for name, entry in before.items() if name in old}


# --------------------------------------------------------------------------- #
# An exported event schema (one file per channel)
# --------------------------------------------------------------------------- #


def exported_mark(schema: Mapping[str, Any] | None, *, where: str) -> int | None:
    """The revision a committed exported schema carries: ``None`` for no file,
    ``0`` a draft, ``1`` or more released; a file with no key is legacy, so
    released at revision 1."""
    if schema is None:
        return None
    if EVENT_MARK not in schema:
        return FIRST_REVISION
    return _revision(schema[EVENT_MARK], where)


def without_mark(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` without the exported revision (what the producer declares)."""
    return {k: v for k, v in schema.items() if k != EVENT_MARK}


def with_mark(schema: Mapping[str, Any], revision: int) -> dict[str, Any]:
    """``schema`` as exported: carrying ``revision`` (``0``: a draft)."""
    return {**without_mark(schema), EVENT_MARK: revision}


def label(revision: int | None) -> str:
    """How ``sandloom docs`` names a revision (``None``: no mark is known)."""
    if revision is None:
        return "unknown"
    return "draft" if revision == 0 else f"released, revision {revision}"


__all__ = [
    "EVENT_MARK",
    "FIRST_REVISION",
    "RELEASED",
    "ReleaseMarkError",
    "exported_mark",
    "label",
    "next_marks",
    "old_marks",
    "strict_entries",
    "with_mark",
    "without_mark",
]
