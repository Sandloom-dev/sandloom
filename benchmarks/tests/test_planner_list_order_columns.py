# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""``ListOperation`` order columns are read once — pages and cursors unchanged.

A list's order values (``order_by`` + the primary key) used to be selected a
SECOND time after the DTO's columns, even when the DTO already projects them.
Now a projected order column is read once, at its DTO position
(``Plan.order_index``), and only the others are appended (``order_width``).

Proven here, on the S5 stand (FORCEd RLS, NOBYPASSRLS role, pool of one), for
both executors, for DTOs projecting ALL, SOME and NONE of the order columns,
ascending and descending, with ties on every order column but the primary key
and NULLs in a projected non-order column:

* every page holds the same rows, in the same order, as an independent SQL
  reference (``ORDER BY created_at, rank, id``);
* every ``next_cursor`` is byte-identical to the cursor of the reference's
  last-row position (what the previous row shape produced), and the digest of
  every cursor of every walk equals the one recorded on ``main`` before the
  change (``f21f617c``) — existing cursors stay valid across a deploy;
* a cursor minted before the change continues the walk identically;
* the plan selects each order column once (both executors' row shape).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import DateTime, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from test_s5_secure_read import (  # noqa: F401 - fixtures
    ROLE,
    S5,
    A,
    B,
    admin,
    pg_proxy,
    s5,
)

from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.orm import Base, ListOperation, TenantScoped, session_reads
from sandloom.core.planner import Reads, encode_cursor, plan_for, read_scope
from sandloom.core.config.settings import settings
from sandloom.core.tenant import bind_tenant
from sandloom.core.unit_of_work import db_session

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
SECRET = "list-order-columns-fixed-cursor-secret-0123456789"


class LoItem(TenantScoped, Base):
    __tablename__ = "bench_lo_items"
    __table_args__ = (
        Index("ix_bench_lo_items_grp_created_rank", "grp", "created_at", "rank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    grp: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AllOut(BaseModel):  # every order column (+ the pk), in another order
    rank: int
    id: uuid.UUID
    note: str | None
    created_at: datetime
    grp: int


class SomeOut(BaseModel):  # created_at and the pk, not rank
    note: str | None
    created_at: datetime
    id: uuid.UUID


class NoneOut(BaseModel):  # no order column at all
    note: str | None
    grp: int


DTOS = {"all": AllOut, "some": SomeOut, "none": NoneOut}
OPS = {
    (dto, desc): ListOperation(
        entity=LoItem,
        result=result,
        by=("grp",),
        order_by=("created_at", "rank"),
        descending=desc,
        max_limit=100,
        name=f"test.lo.{dto}.{'desc' if desc else 'asc'}",
    )
    for dto, result in DTOS.items()
    for desc in (False, True)
}
#: order values appended after the DTO's fields (created_at, rank, id)
EXTRA = {"all": 0, "some": 1, "none": 3}

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LIMITS = (1, 3, 7, 100)

#: sha256 over every cursor of every walk (``LIMITS``, grp 1 then 2, tenant A)
#: per operation, recorded on ``main`` (f21f617c, order columns selected twice;
#: both executors and both entry points gave the same digest there).
#: Re-recorded for the Sandloom cursor MAC domain on 2026-10-28. Restoring
#: only the pre-rename domain reproduced all six original digests (36 tests);
#: row order, payload shape and pagination remain checked against independent SQL.
GOLDEN = {
    "test.lo.all.asc": "45fe74da398312139a64c650a58776d81aa05bd55864634e2f9131e7f7953783",
    "test.lo.all.desc": "dda38097d6cd85e33fb9432a9b228600a7536b1fe9376e83e4768fa7cdbed82d",
    "test.lo.some.asc": "a6bbf75d78dd7b6da52b302581d41e33faf7674a3ab2ff29954749806af7ef1b",
    "test.lo.some.desc": "01c9a3e2902202a5904b29e631db4019d3ff18e09c4a4ac11fdffef09130d0b3",
    "test.lo.none.asc": "c93ad80dd262d82d4e9cdfa4bf29d08a03de790fe49b38551ed07a90e199f213",
    "test.lo.none.desc": "3d5fa3d195874205100454404da522b85bd4aa575018a11f65f2aa4f193a8210",
}


def _rows() -> list[tuple[Any, ...]]:
    """(id, tenant, grp, created_at, rank, note): ties on created_at AND rank
    (only the pk breaks them), NULL notes, a second group and tenant B."""
    out = []
    for i in range(40):
        rid = uuid.UUID(int=(0x10 << 120) + (i * 7919) % 211)  # pk order != i
        out.append(
            (
                rid,
                A,
                1 if i < 30 else 2,
                T0 + timedelta(seconds=i // 4),  # 4-way created_at ties
                (i // 2) % 3,  # rank ties inside a created_at tie
                None if i % 3 == 0 else f"n{i}",
            )
        )
    for i in range(10):  # tenant B, same group: never visible to A
        out.append(
            (uuid.UUID(int=(0x20 << 120) + i), B, 1, T0, 0, f"b{i}"),
        )
    return out


DDL = [
    "DROP TABLE IF EXISTS bench_lo_items CASCADE",
    """CREATE TABLE bench_lo_items (
        id UUID PRIMARY KEY, tenant_id UUID NOT NULL, grp INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, rank INTEGER NOT NULL, note VARCHAR(32))""",
    "CREATE INDEX ix_bench_lo_items_grp_created_rank "
    "ON bench_lo_items (grp, created_at, rank)",
    *enable_tenant_rls_statements("bench_lo_items"),
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON bench_lo_items TO {ROLE}",
]


@pytest_asyncio.fixture
async def lo(s5: S5, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[S5]:
    monkeypatch.setattr(settings, "planner_cursor_secret", SECRET)
    for sql in DDL:
        await s5.admin.execute(sql)
    await s5.admin.executemany(
        "INSERT INTO bench_lo_items VALUES ($1, $2, $3, $4, $5, $6)", _rows()
    )
    yield s5
    await s5.admin.execute("DROP TABLE IF EXISTS bench_lo_items CASCADE")


async def _reference(
    s5: S5, grp: int, desc: bool
) -> list[tuple[dict[str, Any], tuple[Any, ...]]]:
    """(row as a dict, its (created_at, rank, id) position), in list order."""
    d = "DESC" if desc else "ASC"
    rows = await s5.admin.fetch(
        "SELECT id, grp, created_at, rank, note FROM bench_lo_items "
        f"WHERE tenant_id = $1 AND grp = $2 "
        f"ORDER BY created_at {d}, rank {d}, id {d}",
        A,
        grp,
    )
    return [(dict(r), (r["created_at"], r["rank"], r["id"])) for r in rows]


async def _walk(
    sandloom_core: Any, op: ListOperation[Any], limit: int, grp: int, *, scoped: bool
) -> tuple[list[list[Any]], list[str | None]]:
    core = sandloom_core.runtime_view()
    pages: list[list[Any]] = []
    cursors: list[str | None] = []
    cursor = None
    with bind_tenant(A):
        async with read_scope(core, "lo") as shared:
            while True:
                reads = shared if scoped else Reads(core)
                page = await reads.page(op, limit=limit, cursor=cursor, grp=grp)
                pages.append(list(page.items))
                cursors.append(page.next_cursor)
                cursor = page.next_cursor
                if cursor is None:
                    return pages, cursors


def _expected(
    ref: list[tuple[dict[str, Any], tuple[Any, ...]]],
    op: ListOperation[Any],
    limit: int,
    grp: int,
) -> tuple[list[list[Any]], list[str | None]]:
    pages: list[list[Any]] = []
    cursors: list[str | None] = []
    i = 0
    while True:
        chunk = ref[i : i + limit]
        pages.append([op.result.model_validate(r) for r, _ in chunk])
        more = len(ref) > i + limit
        cursors.append(encode_cursor(op, [grp], A, chunk[-1][1]) if more else None)
        if not more:
            return pages, cursors
        i += limit


@BOTH
@pytest.mark.parametrize("dto", list(DTOS))
@pytest.mark.parametrize("desc", [False, True], ids=["asc", "desc"])
@pytest.mark.parametrize("scoped", [False, True], ids=["tx", "scope"])
async def test_pages_and_cursors_match_the_reference(
    lo: S5, sandloom_core: Any, dto: str, desc: bool, scoped: bool
) -> None:
    op = OPS[(dto, desc)]
    digest = hashlib.sha256()
    for grp in (1, 2):
        ref = await _reference(lo, grp, desc)
        assert len(ref) == (30 if grp == 1 else 10)
        for limit in LIMITS:
            got = await _walk(sandloom_core, op, limit, grp, scoped=scoped)
            want = _expected(ref, op, limit, grp)
            assert got[0] == want[0], (op.name, grp, limit)  # rows, order
            assert got[1] == want[1], (op.name, grp, limit)  # cursor bytes
            for c in got[1]:
                digest.update((c or "-").encode() + b"\n")
    assert digest.hexdigest() == GOLDEN[op.name], (op.name, digest.hexdigest())


@BOTH
@pytest.mark.parametrize("dto", list(DTOS))
async def test_a_cursor_minted_before_the_change_continues_the_walk(
    lo: S5, sandloom_core: Any, dto: str
) -> None:
    """A cursor of the reference position (= the old row shape's cursor) is
    accepted and continues exactly after that row — also in a Unit of Work."""
    op = OPS[(dto, True)]
    ref = await _reference(lo, 1, True)
    old_cursor = encode_cursor(op, [1], A, ref[4][1])
    want = [op.result.model_validate(r) for r, _ in ref[5:11]]
    core = sandloom_core.runtime_view()
    with bind_tenant(A):
        page = await Reads(core).page(op, limit=6, cursor=old_cursor, grp=1)
        assert page.items == want
        assert page.next_cursor == encode_cursor(op, [1], A, ref[10][1])
        async with db_session() as db:  # the general form on the UoW session
            page = await session_reads(db).page(op, limit=6, cursor=old_cursor, grp=1)
        assert page.items == want
        assert page.next_cursor == encode_cursor(op, [1], A, ref[10][1])


@BOTH
@pytest.mark.parametrize("dto", list(DTOS))
async def test_each_order_column_is_selected_once(
    lo: S5, sandloom_core: Any, dto: str
) -> None:
    op = OPS[(dto, False)]
    plan = plan_for(sandloom_core.runtime_view(), op)
    want = "optimized" if lo.variant == "planopt" else "general"
    assert plan.executor == want, plan.reason
    assert plan.order_width == EXTRA[dto]
    assert len(plan.order_index) == 3
    fields = plan.fields
    for pos, name in zip(plan.order_index, ("created_at", "rank", "id"), strict=True):
        if pos < len(fields):
            assert fields[pos] == name  # read from its DTO position
    assert plan.compiled.sql is not None  # generated in both modes
    head = plan.compiled.sql.split(" FROM ")[0]
    for col in ("created_at", "rank", "id"):
        assert head.count(f"e.{col}") == 1, head
    general = str(plan.compiled.stmt).split("FROM")[0]
    for col in ("created_at", "rank", "id"):
        assert general.count(f"bench_lo_items.{col}") == 1, general
