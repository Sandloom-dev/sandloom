"""No ``await db().refresh(row)`` after a flush: ``Base`` reads back what the
database set (``eager_defaults``), a ``default=new_id`` key exists the moment the
model is constructed, and ``Versioned`` counts a version up in the database.

Runs on the no-Redis lane (``src/sandloom/core/tests``) over in-memory SQLite:
``RETURNING`` is what the PostgreSQL path uses too.
"""

from __future__ import annotations

import gc
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, ForeignKey, MetaData, create_engine, inspect, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    registry,
    relationship,
)
from sqlalchemy.orm.exc import StaleDataError, UnmappedClassError

from sandloom.core.cli.extract import module_tables
from sandloom.core.cli.migrate_rls import tenant_tables
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.loader.manifest import module_manifest
from sandloom.core.orm import module_feature_uses
from sandloom.core.sql_alchemy.base import Base, base_mappers
from sandloom.core.sql_alchemy.columns import (
    CreatedAt,
    UpdatedAt,
    Versioned,
    new_id,
)
from sandloom.core.testing.migrations import model_tables
from sandloom.core.testing.static_checks import tenant_key_problems


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
    status: Mapped[str] = mapped_column(default="open")


class Shape(_Models):
    """A model with its own ``__mapper_args__`` (polymorphic inheritance): a
    ``__mapper_args__`` on ``Base`` would be replaced by it."""

    __tablename__ = "dx_eager_shapes"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "shape"}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str]
    title: Mapped[str]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class Circle(Shape):
    __mapper_args__ = {"polymorphic_identity": "circle"}


class OptedOut(_Models):
    """A model that names ``eager_defaults=False`` keeps that choice."""

    __tablename__ = "dx_eager_opted_out"
    __mapper_args__ = {"eager_defaults": False}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str]
    updated_at: Mapped[UpdatedAt]


class Counter(_Models):
    """A primary key that is not a ``new_id`` one: left to the database."""

    __tablename__ = "dx_eager_counters"

    id: Mapped[int] = mapped_column(primary_key=True)


class Tagged(_Models):
    """A callable default that is not ``new_id``: not touched at construction."""

    __tablename__ = "dx_eager_tagged"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class _Joined(Base):
    """A metadata of its own for the joined-table models."""

    __abstract__ = True
    metadata = MetaData()


class Asset(Versioned, _Joined):
    """A joined-table parent: ``version`` lives in ``dx_eager_assets``."""

    __tablename__ = "dx_eager_assets"
    __mapper_args__ = {
        "polymorphic_on": "kind",
        "polymorphic_identity": "asset",
        "version_id_col": Versioned.version,
        "version_id_generator": False,
    }

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str]
    title: Mapped[str]


class Video(Asset):
    """Its child: ``duration`` lives in ``dx_eager_videos``, another table."""

    __tablename__ = "dx_eager_videos"
    __mapper_args__ = {"polymorphic_identity": "video"}

    id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dx_eager_assets.id"), primary_key=True
    )
    duration: Mapped[int]


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


@pytest.mark.parametrize("model", [Shape, Circle], ids=["parent", "child"])
def test_a_model_with_its_own_mapper_args_keeps_eager_defaults(
    session: Session, model: type[Shape]
) -> None:
    """Its own ``__mapper_args__`` replaces whatever ``Base`` declares, so
    ``created_at`` and ``updated_at`` would be unloaded (a lazy load that fails
    under ``AsyncSession``) if ``Base`` set the option there."""
    shape = model(title="a")
    session.add(shape)
    session.flush()
    assert not {"created_at", "updated_at"} & _unloaded(shape)
    shape.title = "b"
    session.flush()
    assert "updated_at" not in _unloaded(shape)


def test_a_model_that_names_eager_defaults_false_keeps_its_choice(
    session: Session,
) -> None:
    row = OptedOut(title="a")
    session.add(row)
    session.flush()
    row.title = "b"
    session.flush()
    assert "updated_at" in _unloaded(row)


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


def _seed_doc(engine: Engine) -> uuid.UUID:
    doc_id = new_id()
    with Session(engine) as seed:
        seed.add(Doc(id=doc_id, title="a"))
        seed.commit()
    return doc_id


def _state(engine: Engine, doc_id: uuid.UUID) -> tuple[str, str, int]:
    with Session(engine) as check:
        row = check.get(Doc, doc_id)
        assert row is not None
        return row.title, row.status, row.version


def test_a_stale_update_is_refused_instead_of_published_with_old_columns(
    tmp_path: Path,
) -> None:
    """The race shape: two units read version 1; one renames and commits (2);
    the other sets the status from its old read. Counting alone would write
    version 3 and hand the event the old title; the check refuses the write."""
    engine = create_engine(f"sqlite:///{tmp_path}/race.db")
    _Models.metadata.create_all(engine)
    doc_id = _seed_doc(engine)
    with Session(engine) as first, Session(engine) as second:
        renamed = first.get(Doc, doc_id)
        closing = second.get(Doc, doc_id)
        assert renamed is not None and closing is not None
        renamed.title = "renamed"
        first.commit()
        closing.status = "closed"
        with pytest.raises(StaleDataError):
            second.flush()
    assert _state(engine, doc_id) == ("renamed", "open", 2)


def test_a_unit_that_reads_after_the_other_committed_publishes_the_whole_state(
    tmp_path: Path,
) -> None:
    """What ``with_for_update()`` waits for on a database with row locks (the
    order stands in for it here): the second unit reads the committed title."""
    engine = create_engine(f"sqlite:///{tmp_path}/ordered.db")
    _Models.metadata.create_all(engine)
    doc_id = _seed_doc(engine)
    with Session(engine) as first:
        renamed = first.get(Doc, doc_id)
        assert renamed is not None
        renamed.title = "renamed"
        first.commit()
    with Session(engine) as second:
        closing = second.execute(
            select(Doc).where(Doc.id == doc_id).with_for_update()
        ).scalar_one()
        closing.status = "closed"
        second.flush()
        assert (closing.title, closing.status, closing.version) == (
            "renamed",
            "closed",
            3,
        )


@pytest.mark.parametrize(
    "column", ["duration", "title"], ids=["child_table_only", "parent_table_only"]
)
def test_a_joined_table_update_counts_the_version_whichever_table_changed(
    column: str,
) -> None:
    """``version`` lives in the parent table: an update that changes only the
    child's own columns sends the parent table no statement, so the database
    never counted it (and no stale-write check ran)."""
    engine = create_engine("sqlite://")
    _Joined.metadata.create_all(engine)
    with Session(engine) as session:
        video = Video(title="clip", duration=1)
        session.add(video)
        session.flush()
        assert video.version == 1
        setattr(video, column, 2 if column == "duration" else "cut")
        session.flush()
        assert "version" not in _unloaded(video)  # read back with the update
        assert video.version == 2
        session.expire(video)
        assert video.version == 2
        session.flush()  # nothing changed since
        assert video.version == 2


def test_a_stale_child_only_update_of_a_joined_table_is_refused(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path}/joined.db")
    _Joined.metadata.create_all(engine)
    video_id = new_id()
    with Session(engine) as seed:
        seed.add(Video(id=video_id, title="clip", duration=1))
        seed.commit()
    with Session(engine) as first, Session(engine) as second:
        cut = first.get(Video, video_id)
        trimmed = second.get(Video, video_id)
        assert cut is not None and trimmed is not None
        cut.duration = 5
        first.commit()
        trimmed.duration = 9
        with pytest.raises(StaleDataError):
            second.flush()
    with Session(engine) as check:
        row = check.get(Video, video_id)
        assert row is not None
        assert (row.duration, row.version) == (5, 2)


def test_constructing_a_model_does_not_configure_an_unrelated_registry() -> None:
    """Another registry with a class still being declared (a relationship to a
    name not defined yet) must not fail this construction: only the model's
    own registry is configured."""
    partial = registry()

    @partial.mapped
    class Half:
        __tablename__ = "dx_eager_half"

        id: Mapped[int] = mapped_column(primary_key=True)
        other = relationship("NotDeclaredYet")

    class _Isolated(Base):
        __abstract__ = True
        metadata = MetaData()

    class Whole(_Isolated):
        __tablename__ = "dx_eager_whole"

        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)

    try:
        assert isinstance(Whole().id, uuid.UUID)
    finally:
        partial.dispose()


def test_a_versioned_model_that_drops_the_check_is_refused_when_declared() -> None:
    """Its own ``__mapper_args__`` replaces the mixin's ``version_id_col``: the
    version would count with no stale-write check, so the declaration fails."""

    class _Isolated(Base):
        __abstract__ = True
        metadata = MetaData()

    with pytest.raises(TypeError, match="stale-write check"):

        class Unchecked(Versioned, _Isolated):
            __tablename__ = "dx_eager_unchecked"
            __mapper_args__ = {"eager_defaults": True}

            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)


def test_a_versioned_model_that_names_the_check_itself_is_accepted() -> None:
    class _Isolated(Base):
        __abstract__ = True
        metadata = MetaData()

    class Checked(Versioned, _Isolated):
        __tablename__ = "dx_eager_checked"
        __mapper_args__ = {
            "version_id_col": Versioned.version,
            "version_id_generator": False,
        }

        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)

    assert inspect(Checked).version_id_col is not None


def test_a_refused_declaration_does_not_break_the_readers_of_the_models() -> None:
    """A refused class stays in ``Base.registry`` half built until the garbage
    collector frees it, and ``Base.registry.mappers`` raises meanwhile: the
    module load (the manifest, the database features) and the migration and
    tenant checks read the models through ``base_mappers`` and still see every
    mapped one. Regression: main went red when a later test, on the same xdist
    worker, loaded a module while ``Unchecked`` above was still registered."""
    package = "dx_eager_registry"

    class _Isolated(Base):
        __abstract__ = True
        metadata = MetaData()

    class Good(_Isolated):
        __tablename__ = f"{package}_good"

        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)

    Good.__module__ = f"{package}.models"  # as if declared in the module package

    with pytest.raises(TypeError, match="stale-write check"):

        class Refused(Versioned, _Isolated):
            __tablename__ = f"{package}_refused"
            __mapper_args__ = {"eager_defaults": True}

            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)

    # held here, so the half-built class cannot be collected under the test
    refused = [c for c in _Isolated.__subclasses__() if c.__name__ == "Refused"]
    assert len(refused) == 1
    with pytest.raises(UnmappedClassError):
        assert Base.registry.mappers

    descriptor = ModuleDescriptor(name=package, version="0", migrations_path="/none")
    core = SimpleNamespace(module_descriptors=[descriptor])
    try:
        assert {m.class_ for m in base_mappers()} >= {Good}
        assert refused[0] not in {m.class_ for m in base_mappers()}
        assert [
            m.table for m in module_manifest(descriptor, package=package).models
        ] == [f"{package}_good"]
        assert module_feature_uses(package) == []
        assert tenant_tables(package) == {}
        assert module_tables(descriptor) == (f"{package}_good",)
        assert model_tables(core)[f"{package}_good"].module == package
        assert isinstance(tenant_key_problems(), list)
    finally:
        del refused
        gc.collect()  # the half-built class goes, as it would sooner or later
