"""No ``await db().refresh(row)`` after a flush: ``Base`` reads back what the
database set (``eager_defaults``), a ``default=new_id`` key exists the moment the
model is constructed, and ``Versioned`` counts a version up in the database.

Runs on the no-Redis lane (``src/sandloom/core/tests``) over in-memory SQLite:
``RETURNING`` is what the PostgreSQL path uses too.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import MetaData, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.columns import (
    CreatedAt,
    UpdatedAt,
    Versioned,
    new_id,
)


class _Models(Base):
    """The shared ``Base`` behaviour on a metadata of its own."""

    __abstract__ = True
    metadata = MetaData()


class _Plain(DeclarativeBase):
    """A base without ``eager_defaults``: what the old default did."""


class Note(_Models):
    __tablename__ = "dx_eager_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class PlainNote(_Plain):
    __tablename__ = "dx_eager_plain_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class Doc(Versioned, _Models):
    __tablename__ = "dx_eager_docs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str]


class Counter(_Models):
    """A primary key that is not a ``new_id`` one: left to the database."""

    __tablename__ = "dx_eager_counters"

    id: Mapped[int] = mapped_column(primary_key=True)


class Tagged(_Models):
    """A callable default that is not ``new_id``: not touched at construction."""

    __tablename__ = "dx_eager_tagged"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    _Models.metadata.create_all(engine)
    _Plain.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _unloaded(row: object) -> set[str]:
    return set(inspect(row).unloaded)


# --------------------------------------------------------------------------- #
# eager_defaults: the timestamps come back with the flush
# --------------------------------------------------------------------------- #


def test_the_insert_flush_loads_created_at_and_updated_at(session: Session) -> None:
    note = Note(title="a")
    session.add(note)
    session.flush()
    assert not {"created_at", "updated_at"} & _unloaded(note)
    assert isinstance(note.created_at, datetime) and note.created_at.tzinfo


def test_the_update_flush_loads_updated_at(session: Session) -> None:
    note = Note(title="a")
    session.add(note)
    session.flush()
    note.title = "b"
    session.flush()
    assert "updated_at" not in _unloaded(note)
    assert note.updated_at >= note.created_at


def test_without_eager_defaults_the_flush_leaves_them_unloaded(
    session: Session,
) -> None:
    """The bite: the checks above fail on a base that lacks ``eager_defaults``
    (the state before this change, which made authors ``refresh``)."""
    note = PlainNote(title="a")
    session.add(note)
    session.flush()
    note.title = "b"
    session.flush()
    assert "updated_at" in _unloaded(note)


# --------------------------------------------------------------------------- #
# The id at construction
# --------------------------------------------------------------------------- #


def test_a_new_id_key_exists_at_construction() -> None:
    first, second = Note(title="a"), Note(title="b")
    assert isinstance(first.id, uuid.UUID) and first.id.version == 7
    assert first.id < second.id  # creation order, as a flushed id would be


def test_the_id_argument_wins() -> None:
    given = uuid.uuid4()
    assert Note(id=given, title="a").id == given


def test_the_constructed_id_is_the_row_id(session: Session) -> None:
    note = Note(title="a")
    before = note.id
    session.add(note)
    session.flush()
    assert note.id == before
    assert session.get(Note, before) is note


def test_a_key_that_is_not_new_id_is_left_to_the_database() -> None:
    assert Counter().id is None
    assert Tagged().id is None


def test_the_first_construction_in_a_fresh_interpreter_assigns_the_id() -> None:
    """The listener runs before the mapper configures itself: a model that is
    the first thing constructed in a process must not fail or skip its id."""
    code = (
        "import uuid\n"
        "from sqlalchemy.orm import Mapped, mapped_column\n"
        "from sandloom.sdk import Base, new_id\n"
        "class Item(Base):\n"
        "    __tablename__ = 'first_items'\n"
        "    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)\n"
        "assert isinstance(Item().id, uuid.UUID)\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


# --------------------------------------------------------------------------- #
# Versioned: the database counts
# --------------------------------------------------------------------------- #


def test_versioned_starts_at_one_and_counts_each_update(session: Session) -> None:
    doc = Doc(title="a")
    session.add(doc)
    session.flush()
    assert doc.version == 1
    doc.title = "b"
    session.flush()
    assert "version" not in _unloaded(doc)  # read back with the update: no refresh
    assert doc.version == 2
    doc.title = "c"
    session.flush()
    assert doc.version == 3


def test_a_flush_that_changes_nothing_keeps_the_version(session: Session) -> None:
    doc = Doc(title="a")
    session.add(doc)
    session.flush()
    doc.title = "b"
    session.flush()
    session.flush()
    doc.title = "b"  # the same value is no change
    session.flush()
    assert doc.version == 2


def test_the_version_is_what_the_database_holds(session: Session) -> None:
    doc = Doc(title="a")
    session.add(doc)
    session.flush()
    doc.title = "b"
    session.flush()
    session.expire(doc)
    assert doc.version == 2
