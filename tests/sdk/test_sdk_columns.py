"""``new_id``, ``CreatedAt`` and ``UpdatedAt``: the framework's idiom for ids,
timestamps and a deterministic list order (``sandloom.sdk``).

Rows one transaction writes must not tie: ``now()`` is the transaction's
start, so the timestamps default to ``clock_timestamp()``; ids are UUIDv7, so
``order_by(id)`` is the creation order even within one millisecond.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import render_python_code
from alembic.operations import ops
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateTable

import sandloom.core.sql_alchemy.columns as columns
from sandloom.sdk import CreatedAt, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt


class _Base(DeclarativeBase):
    """Its own metadata: the shared ``Base`` stays free of test tables."""


class _Note(_Base):
    __tablename__ = "sdkcol_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(sa.String(50))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class _Tagged(TenantScoped, _Base):
    __tablename__ = "sdkcol_tagged"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    created_at: Mapped[CreatedAt]


# --------------------------------------------------------------------------- #
# new_id: UUIDv7
# --------------------------------------------------------------------------- #


def _ms(value: uuid.UUID) -> int:
    return value.int >> 80


def test_new_id_is_a_uuid7_stamped_with_the_current_millisecond() -> None:
    before = time.time_ns() // 1_000_000
    value = new_id()
    after = time.time_ns() // 1_000_000
    assert isinstance(value, uuid.UUID)
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert before <= _ms(value) <= after


@pytest.mark.parametrize("generate", [new_id, columns._uuid7], ids=["new_id", "rfc"])
def test_ids_of_a_burst_strictly_increase(generate: Any) -> None:
    ids = [generate() for _ in range(20_000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    # Thousands of ids per millisecond: many share their timestamp.
    assert len({_ms(i) for i in ids}) < len(ids)


def test_the_fallback_orders_one_millisecond_and_a_clock_step_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 3.11-3.13 implementation (3.14 uses the stdlib's ``uuid.uuid7``):
    a frozen clock, then one that steps back, still yields increasing ids
    with the version and variant bits set."""
    now = [1_800_000_000_000 * 1_000_000]
    monkeypatch.setattr(columns.time, "time_ns", lambda: now[0])
    monkeypatch.setattr(columns, "_last_ms", -1)
    frozen = [columns._uuid7() for _ in range(1_000)]
    now[0] -= 5_000 * 1_000_000  # NTP steps the clock back 5 s
    stepped_back = [columns._uuid7() for _ in range(10)]
    ids = frozen + stepped_back
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert {_ms(i) for i in ids} == {1_800_000_000_000}
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)


def test_the_fallback_moves_to_the_next_millisecond_when_the_counter_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(columns.time, "time_ns", lambda: 7 * 1_000_000)
    monkeypatch.setattr(columns, "_last_ms", 7)
    monkeypatch.setattr(columns, "_last_counter", columns._COUNTER_MAX)
    last = uuid.UUID(int=(7 << 80) | (0xFFF << 64) | (0x3FFF_FFFF << 32))
    following = columns._uuid7()
    assert _ms(following) == 8 and following > last


_CHILD = "from sandloom.sdk import new_id; print(new_id())"


def test_ids_of_different_processes_order_by_time() -> None:
    """Across processes the millisecond orders ids: an id made after another
    process's (a few ms later) sorts after it, and the reverse."""
    child = uuid.UUID(
        subprocess.run(
            [sys.executable, "-c", _CHILD], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    time.sleep(0.003)
    mine = new_id()
    time.sleep(0.003)
    later = uuid.UUID(
        subprocess.run(
            [sys.executable, "-c", _CHILD], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    assert child < mine < later
    assert _ms(child) < _ms(mine) < _ms(later)


# --------------------------------------------------------------------------- #
# CreatedAt / UpdatedAt: DDL and Alembic autogenerate
# --------------------------------------------------------------------------- #


def _ddl(table: sa.Table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_the_timestamp_columns_default_to_clock_timestamp() -> None:
    ddl = _ddl(_Note.__table__)  # type: ignore[arg-type]
    assert (
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL" in ddl
    )
    assert (
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL" in ddl
    )
    assert "now()" not in ddl
    updated = _Note.__table__.c.updated_at
    assert updated.onupdate is not None
    assert "clock_timestamp" in str(updated.onupdate.arg)  # type: ignore[attr-defined]
    assert _Note.__table__.c.created_at.onupdate is None
    assert _Note.__table__.c.id.default.arg.__name__ == "new_id"  # type: ignore[union-attr]


def test_the_columns_compose_with_tenant_scoped() -> None:
    """Each table gets its own column (the Annotated column is copied), next
    to ``TenantScoped``'s ``tenant_id``."""
    ddl = _ddl(_Tagged.__table__)  # type: ignore[arg-type]
    assert "tenant_id UUID NOT NULL" in ddl
    assert (
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL" in ddl
    )
    assert _Tagged.__table__.c.created_at is not _Note.__table__.c.created_at


def test_alembic_autogenerate_renders_clock_timestamp() -> None:
    """What ``sandloom migrate new`` writes for a new table with these columns."""
    op = ops.CreateTableOp.from_table(_Note.__table__)  # type: ignore[arg-type]
    code = render_python_code(ops.UpgradeOps(ops=[op]))
    assert (
        "sa.Column('created_at', sa.DateTime(timezone=True), "
        "server_default=sa.text('clock_timestamp()'), nullable=False)" in code
    )
    assert (
        "sa.Column('updated_at', sa.DateTime(timezone=True), "
        "server_default=sa.text('clock_timestamp()'), nullable=False)" in code
    )


# --------------------------------------------------------------------------- #
# In the database: one transaction, distinct times, a deterministic order
# --------------------------------------------------------------------------- #


async def test_rows_of_one_transaction_list_in_creation_order(db: Any) -> None:
    connection = await db.connection()
    await connection.run_sync(_Base.metadata.create_all, tables=[_Note.__table__])
    for title in ("a", "b", "c", "d"):
        db.add(_Note(title=title))
        await db.flush()  # one INSERT each, as one request after another
    newest = sa.select(_Note.title).order_by(_Note.created_at.desc(), _Note.id.desc())
    assert list(await db.scalars(newest)) == ["d", "c", "b", "a"]
    by_id = sa.select(_Note.title).order_by(_Note.id)
    assert list(await db.scalars(by_id)) == ["a", "b", "c", "d"]
    times = list(await db.scalars(sa.select(_Note.created_at)))
    assert len(set(times)) == 4  # clock_timestamp(): no tie in one transaction

    note = await db.scalar(sa.select(_Note).where(_Note.title == "a"))
    note.title = "a2"
    await db.flush()
    await db.refresh(note)
    assert note.updated_at > note.created_at


def test_sandloom_docs_describes_the_idiom() -> None:
    """``sandloom docs CreatedAt`` shows the type and how to map it (not the
    docstring of ``typing.Annotated``); ``sandloom docs recipe list-order``."""
    from sandloom.sdk._prim.docs import describe
    from sandloom.sdk._prim.recipes import _recipe

    created = describe("CreatedAt")
    assert "sandloom.sdk.CreatedAt = Annotated[datetime, ...] (a type)" in created
    assert "tier: beginner" in created
    assert "created_at: Mapped[CreatedAt]" in created
    assert "clock_timestamp()" in describe("UpdatedAt")
    assert "UUIDv7" in describe("new_id")
    recipe = _recipe("list-order")
    assert recipe is not None
    assert "default=new_id" in recipe and "created_at: Mapped[CreatedAt]" in recipe
    assert "order_by(Note.created_at.desc(), Note.id.desc())" in recipe
