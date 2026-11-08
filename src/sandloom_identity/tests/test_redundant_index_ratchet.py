"""Identity's redundant indexes: a remove-only ratchet (B-N19).

The kernel's redundant-index check (SANDLOOM-T012) reports a plain index on
the leading column(s) of a unique key of the same table. An index identity
keeps on purpose carries ``info={"sandloom_known_redundant": "<reason>"}``,
which the check reads; this test is where identity holds that list, so it may
only shrink: drop the index with a revision, then remove it from ``KEPT``.
"""

from sqlalchemy import Index, Table, UniqueConstraint

import sandloom_identity.models  # noqa: F401 - registers identity's tables
from sandloom.sdk import Base

#: The marker the kernel check reads on an Index's ``info``.
MARKER = "sandloom_known_redundant"

#: Redundant indexes identity still keeps; may only lose entries.
KEPT = frozenset({"external_identities.ix_external_identities_provider"})


def _identity_tables() -> list[Table]:
    """The tables of identity's mapped models."""
    return [
        mapper.local_table
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith("sandloom_identity.")
        and isinstance(mapper.local_table, Table)
    ]


def _marked() -> dict[str, Index]:
    """``table.index`` -> Index of identity's indexes carrying the marker."""
    return {
        f"{table.name}.{index.name}": index
        for table in _identity_tables()
        for index in table.indexes
        if MARKER in index.info
    }


def test_identity_marks_no_index_outside_the_ratchet() -> None:
    """A new redundant index is dropped, not marked: the set only shrinks."""
    assert any(t.name == "external_identities" for t in _identity_tables())
    assert set(_marked()) <= KEPT


def test_each_marked_index_states_why_it_is_kept() -> None:
    """The kernel check waives an index only for a non-empty reason."""
    for name, index in _marked().items():
        assert str(index.info[MARKER]).strip(), f"{name}: empty reason"


def test_each_marked_index_is_still_redundant() -> None:
    """A marker on an index that no longer duplicates a unique prefix is stale:
    remove it (and its ``KEPT`` entry)."""
    for name, index in _marked().items():
        table = index.table
        assert isinstance(table, Table)
        cols = tuple(c.name for c in index.columns)
        uniques = [
            tuple(c.name for c in other.columns)
            for other in table.indexes
            if other.unique
        ] + [
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        assert any(u[: len(cols)] == cols for u in uniques), f"{name}: not redundant"
