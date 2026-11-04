"""``WallClock`` and ``UtcDateTime``: ``CreatedAt`` / ``UpdatedAt`` on any database.

On PostgreSQL the DDL, the ORM update and the migration render are exactly
what ``func.clock_timestamp()`` and ``DateTime(timezone=True)`` wrote before
(no migration, no drift); on SQLite the columns are stamped in UTC to the
millisecond and read back timezone-aware.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata, render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations, ops
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.schema import CreateTable

from sandloom.core.migrations.alembic_support import _render_item
from sandloom.core.sql_alchemy.columns import CreatedAt, UpdatedAt, new_id
from sandloom.core.sql_alchemy.portable import (
    UnsupportedSqlForm,
    UtcDateTime,
    WallClock,
)


class _Base(DeclarativeBase):
    pass


class _Stamped(_Base):
    __tablename__ = "wall_clock_stamped"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(sa.String(40))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


_PG = postgresql.dialect()  # type: ignore[no-untyped-call]


def _flat(sql: object) -> str:
    return " ".join(str(sql).split())


def test_postgresql_ddl_is_clock_timestamp_timestamptz() -> None:
    ddl = _flat(CreateTable(_Stamped.__table__).compile(dialect=_PG))
    assert (
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL" in ddl
    )
    assert (
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL" in ddl
    )


def test_postgresql_update_sets_clock_timestamp() -> None:
    # The statement the ORM flushes for a changed row: ``onupdate`` is
    # rendered inline, as func.clock_timestamp() was.
    table = _Stamped.__table__
    sql = _flat(
        sa.update(table)
        .values(title="b")
        .where(table.c.id == uuid.UUID(int=1))
        .compile(dialect=_PG)
    )
    assert sql == (
        "UPDATE wall_clock_stamped SET title=%(title)s::VARCHAR, "
        "updated_at=clock_timestamp() WHERE wall_clock_stamped.id = %(id_1)s::UUID"
    )


def test_sqlite_stamps_utc_milliseconds_and_reads_aware() -> None:
    engine = sa.create_engine("sqlite://")
    ddl = _flat(CreateTable(_Stamped.__table__).compile(dialect=engine.dialect))
    assert "DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now') || '000')" in ddl
    _Base.metadata.create_all(engine)
    before = datetime.now(UTC) - timedelta(seconds=1)
    with Session(engine) as session:
        rows = [_Stamped(title=str(n)) for n in range(3)]
        session.add_all(rows)
        session.flush()
        first = rows[0]
        session.refresh(first)
        assert first.created_at.tzinfo is UTC
        assert before <= first.created_at <= datetime.now(UTC) + timedelta(seconds=1)
        stamped = first.updated_at
        first.title = "changed"
        session.flush()
        session.refresh(first)
        assert first.updated_at >= stamped
        ordered = session.scalars(
            sa.select(_Stamped).order_by(_Stamped.created_at, _Stamped.id)
        ).all()
        # Ties within a millisecond are ordered by the UUIDv7 id.
        assert [r.title for r in ordered] == ["changed", "1", "2"]
        # The stamped text is the text SQLAlchemy binds (6-digit
        # microseconds), so the value read back finds its row again.
        at = first.created_at
        table = _Stamped.__table__
        for condition in (table.c.created_at == at, table.c.created_at >= at):
            found = session.scalars(
                sa.select(table.c.title).where(condition, table.c.id == first.id)
            ).all()
            assert found == ["changed"], condition
        raw = session.scalar(
            sa.text("SELECT created_at FROM wall_clock_stamped WHERE id = :id"),
            {"id": first.id.hex},
        )
        assert len(raw.split(".")[1]) == 6, raw


def test_utc_datetime_writes_utc_and_attaches_it_on_load() -> None:
    engine = sa.create_engine("sqlite://")
    table = sa.Table(
        "wall_clock_times",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("at", UtcDateTime()),
    )
    table.metadata.create_all(engine)
    plus_three = timezone(timedelta(hours=3))
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {"id": 1, "at": datetime(2026, 1, 1, 12, 0, tzinfo=plus_three)},
                {"id": 2, "at": datetime(2026, 1, 1, 12, 0)},  # SQLite: UTC
                {"id": 3, "at": None},
            ],
        )
        got = connection.execute(sa.select(table.c.at).order_by(table.c.id)).all()
    assert [r.at for r in got] == [
        datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        None,
    ]


def test_utc_datetime_ddl_is_timestamptz_on_postgresql() -> None:
    column = sa.Column("at", UtcDateTime())
    table = sa.Table("wall_clock_ddl", sa.MetaData(), column)
    ddl = _flat(CreateTable(table).compile(dialect=_PG))
    assert ddl == "CREATE TABLE wall_clock_ddl ( at TIMESTAMP WITH TIME ZONE )"


def test_a_database_without_a_renderer_refuses_wall_clock() -> None:
    with pytest.raises(UnsupportedSqlForm, match="WallClock"):
        sa.select(WallClock()).compile(dialect=mysql.dialect())  # type: ignore[no-untyped-call]


def test_str_names_no_database() -> None:
    # SQLAlchemy's stringifier (``str(statement)``, logs) is not a database.
    assert str(sa.select(WallClock())) == "SELECT wall_clock() AS wall_clock_1"
    # ...but compiling for SQLAlchemy's bare default dialect still refuses,
    # so no DDL or executed statement carries that text.
    with pytest.raises(UnsupportedSqlForm, match="WallClock"):
        sa.select(WallClock()).compile(dialect=DefaultDialect())


def test_autogenerate_renders_postgresql_types_and_default() -> None:
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"render_item": _render_item}
    )
    code = render_python_code(
        ops.UpgradeOps([ops.CreateTableOp.from_table(_Stamped.__table__)]),
        render_item=_render_item,
        migration_context=context,
    )
    assert "sandloom" not in code
    for name in ("created_at", "updated_at"):
        assert (
            f"sa.Column('{name}', sa.DateTime(timezone=True), "
            "server_default=sa.text('clock_timestamp()'), nullable=False)"
        ) in code


def _test_database_url() -> str:
    from sandloom.core.migrations.urls import to_sync_url
    from sandloom.core.testing.workers import worker_env_values

    values = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    return to_sync_url(values["DATABASE_URL"])  # this worker's own database


def test_alembic_sees_no_change_on_a_clock_timestamp_table() -> None:
    """A table the existing migrations made (``sa.text("clock_timestamp()")``,
    ``timestamptz``) compares equal to ``CreatedAt`` / ``UpdatedAt`` with
    server-default and type comparison on; ``now()`` would not."""
    engine = sa.create_engine(_test_database_url(), poolclass=sa.pool.NullPool)
    found: dict[str, list[object]] = {}
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_server_default": True,
                    "compare_type": True,
                    # This worker's database holds other tables: compare one.
                    "include_name": lambda name, kind, _parent: (
                        kind != "table" or name == "wall_clock_stamped"
                    ),
                },
            )
            op = Operations(context)
            for default in ("clock_timestamp()", "now()"):
                op.create_table(
                    "wall_clock_stamped",
                    sa.Column("id", sa.Uuid(), primary_key=True),
                    sa.Column("title", sa.String(length=40), nullable=False),
                    *(
                        sa.Column(
                            name,
                            sa.DateTime(timezone=True),
                            server_default=sa.text(default),
                            nullable=False,
                        )
                        for name in ("created_at", "updated_at")
                    ),
                )
                found[default] = compare_metadata(context, _Base.metadata)
                op.drop_table("wall_clock_stamped")
            transaction.rollback()
    finally:
        engine.dispose()
    assert found["clock_timestamp()"] == []
    assert len(found["now()"]) == 2  # the comparison is live
