"""Released marks of contracts and events (ADR-0017). Pure: no file, no import
of a module, no git.

A snapshot file records which of its entries are *released* with a revision.
``contracts.snapshot.json`` and ``module.snapshot.json`` hold a top-level
``released`` map (``{name: revision}``); an application's exported event file
holds ``x-sandloom-released`` (the revision of its one channel, ``0`` a draft).

- an entry that an existing file lists as a draft (no mark, with the
  ``released`` key present) or that is new to an existing file is a **draft**:
  rewritten freely, no T004 / T005. A draft is never inferred from a missing
  file: it is named, by the command that creates it (``sandloom new``'s sample,
  ``add module``, ``add contract``, ``add event``) or by
  ``sandloom generate --draft NAME``;
- a **released** entry keeps the strict rule; a compatible change of it moves
  its revision by one (computed by ``sandloom generate``; the number is not
  verified against any history, so a hand edit changes only the number);
- a snapshot file written fresh (none committed: deleted, moved, never
  generated) records every current entry as released at revision 1, so a lost
  file loosens nothing; a file written before the marks existed has no key at
  all: **legacy**, its entries count as released at revision 1 as well.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

#: The key of the ``{name: revision}`` map in a module's snapshot files.
RELEASED = "released"
#: The key of an exported event schema's revision (``0``: a draft).
EVENT_MARK = "x-sandloom-released"
#: The revision a release writes.
FIRST_REVISION = 1


class ReleaseMarkError(ValueError):
    """A mark in a committed file is not a revision."""


def _revision(value: object, where: str, *, lowest: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lowest:
        raise ReleaseMarkError(
            f"{where}: a revision is a whole number of {lowest} or more, got {value!r}"
        )
    return value


def old_marks(
    document: Mapping[str, Any] | None, entries: Mapping[str, Any], *, where: str
) -> dict[str, int]:
    """The released names of a committed snapshot ``document`` (``None``: no
    file is committed, so there is no baseline to compare with: the file is
    written fresh, :func:`next_marks`). ``entries`` are the names the document
    lists: a document with no ``released`` key is legacy and releases them all
    at revision 1. An entry the map lists with ``0`` is refused."""
    if document is None:
        return {}
    if RELEASED not in document:
        return dict.fromkeys(entries, FIRST_REVISION)
    found = document[RELEASED]
    if not isinstance(found, dict):
        raise ReleaseMarkError(f"{where}: 'released' must be an object of revisions")
    zero = [
        str(n)
        for n, r in found.items()
        if r == 0 and isinstance(r, int) and not isinstance(r, bool)
    ]
    if zero:
        raise ReleaseMarkError(
            f"{where}: {', '.join(sorted(zero))} marked released at revision 0; a "
            "draft has no entry in 'released'"
        )
    return {str(n): _revision(r, f"{where}: {n}", lowest=1) for n, r in found.items()}


def next_marks(
    old: Mapping[str, int],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    release: bool,
    fresh: bool = False,
    drafts: Collection[str] = (),
) -> dict[str, int]:
    """The marks to write: a released name keeps its mark, one revision up
    when its entry changed (a compatible change: the caller refused a breaking
    one already); a draft becomes released when ``release``. A name that is
    gone loses its mark.

    ``fresh``: no file is committed, so there is nothing to tell a draft from a
    release: every current name is recorded as released (a deleted or moved
    file never loosens what was protected). ``drafts`` are the names written
    as drafts instead, whatever ``release`` and ``fresh`` say: a draft is only
    ever named. A name that is released in the committed file cannot be named:
    :class:`ReleaseMarkError`."""
    marks: dict[str, int] = {}
    held = sorted(n for n in drafts if n in old)
    if held:
        raise ReleaseMarkError(
            f"{', '.join(held)} is released: a released name is not made a draft "
            "by `--draft` (a new major, `.v(N+1)`, is the way to change it)"
        )
    for name, entry in after.items():
        revision = old.get(name)
        if name in drafts:
            continue
        if revision is None:
            if release or fresh:
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
    return _revision(schema[EVENT_MARK], where, lowest=0)


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
