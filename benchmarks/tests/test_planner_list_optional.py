# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""``ListOperation`` optional equality filters and literal prefix filters.

``optional={arg: column}`` applies ``column = arg`` only when the argument is
not ``None``; ``prefix={arg: column}`` applies ``column [I]LIKE <arg> || '%'``
with the argument matched LITERALLY (``%`` / ``_`` / ``\\`` escaped), only
when it is not ``None``. Proven here on the S5 stand (FORCEd RLS, NOBYPASSRLS
role, pool of one), for both executors, against an independent Python
reference:

* every filter combination (absent / matching / non-matching status, prefixes
  in every case, wildcard characters, the empty prefix) returns the reference
  rows in the reference order, page by page at limits 1 / 3 / 100, on a fresh
  transaction per page, in a shared read scope and on a Unit of Work;
* ``prefix_ignore_case`` is ``ILIKE``; without it the match is case-sensitive;
* a cursor is bound to its filter values: reused with other filters it is
  refused (400) before any I/O;
* each combination is its own statement (no ``$n IS NULL OR ...``), the
  optimized executor stays optimized, the tenant predicate and RLS apply.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid
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
from sandloom.core.planner import InvalidCursor, Reads, like_prefix, plan_for, read_scope
from sandloom.core.config.settings import settings
from sandloom.core.tenant import bind_tenant
from sandloom.core.unit_of_work import db_session

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)


class OptItem(TenantScoped, Base):
    __tablename__ = "bench_opt_items"
    __table_args__ = (Index("ix_bench_opt_items_grp_created", "grp", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    grp: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class OptOut(BaseModel):
    id: uuid.UUID
    status: str
    name: str


def _op(ignore_case: bool) -> ListOperation[OptOut]:
    return ListOperation(
        entity=OptItem,
        result=OptOut,
        by=("grp",),
        optional={"status": "status"},
        prefix={"starts": "name"},
        prefix_ignore_case=ignore_case,
        order_by=("created_at",),
        descending=True,
        max_limit=100,
        name=f"test.opt.{'ilike' if ignore_case else 'like'}",
    )


OPS = {True: _op(True), False: _op(False)}
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NAMES = [
    "Dev board",
    "dev tools",
    "DEVOPS",
    "Design",
    "devil",
    "100% done",
    "100 percent",
    "a_b",
    "axb",
    "a%b",
    "a\\b",
    "a\\\\b",
    "zeta",
    "",
]
STATUSES = ["open", "closed", "open"]


def _rows() -> list[tuple[Any, ...]]:
    """(id, tenant, grp, status, name, created_at); created_at ties of 2."""
    out = []
    for i in range(3 * len(NAMES)):
        rid = uuid.UUID(int=(0x30 << 120) + (i * 7919) % 389)
        out.append(
            (
                rid,
                A,
                1 if i < 2 * len(NAMES) else 2,
                STATUSES[i % 3],
                NAMES[i % len(NAMES)],
                T0 + timedelta(seconds=i // 2),
            )
        )
    for i, name in enumerate(NAMES):  # tenant B, same group: never visible
        out.append((uuid.UUID(int=(0x31 << 120) + i), B, 1, "open", name, T0))
    return out


ROWS = _rows()
DDL = [
    "DROP TABLE IF EXISTS bench_opt_items CASCADE",
    """CREATE TABLE bench_opt_items (
        id UUID PRIMARY KEY, tenant_id UUID NOT NULL, grp INTEGER NOT NULL,
        status VARCHAR(16) NOT NULL, name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL)""",
    "CREATE INDEX ix_bench_opt_items_grp_created ON bench_opt_items (grp, created_at)",
    *enable_tenant_rls_statements("bench_opt_items"),
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON bench_opt_items TO {ROLE}",
]

STATUS_CASES = [None, "open", "closed", "nosuch"]
PREFIX_CASES = [
    None,
    "",
    "dev",
    "DEV",
    "De",
    "%",
    "_",
    "100%",
    "a_",
    "a%",
    "a\\",
    "a\\\\",
    "zz",
]


@pytest_asyncio.fixture
async def opt(s5: S5) -> AsyncIterator[S5]:
    for sql in DDL:
        await s5.admin.execute(sql)
    await s5.admin.executemany(
        "INSERT INTO bench_opt_items VALUES ($1, $2, $3, $4, $5, $6)", ROWS
    )
    yield s5
    await s5.admin.execute("DROP TABLE IF EXISTS bench_opt_items CASCADE")


def _reference(
    grp: int, status: str | None, starts: str | None, ignore_case: bool
) -> list[OptOut]:
    """Independent reference: Python filters, ``ORDER BY created_at, id`` DESC."""

    def keep(r: tuple[Any, ...]) -> bool:
        _id, tenant, g, st, name, _t = r
        if tenant != A or g != grp:
            return False
        if status is not None and st != status:
            return False
        if starts is not None:
            if ignore_case:
                return name.lower().startswith(starts.lower())
            return name.startswith(starts)
        return True

    rows = sorted(filter(keep, ROWS), key=lambda r: (r[5], r[0]), reverse=True)
    return [OptOut(id=r[0], status=r[3], name=r[4]) for r in rows]


async def _walk(
    reads_of: Any, op: ListOperation[OptOut], limit: int, **key: Any
) -> list[OptOut]:
    out: list[OptOut] = []
    cursor = None
    while True:
        page = await reads_of().page(op, limit=limit, cursor=cursor, **key)
        assert len(page.items) <= limit
        out.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return out


async def test_like_prefix_escapes_the_wildcards() -> None:
    assert like_prefix("") == "%"
    assert like_prefix("dev") == "dev%"
    assert like_prefix("100%") == "100\\%%"
    assert like_prefix("a_b") == "a\\_b%"
    assert like_prefix("a\\") == "a\\\\%"


@BOTH
@pytest.mark.parametrize("ignore_case", [True, False], ids=["ilike", "like"])
async def test_every_filter_combination_matches_the_reference(
    opt: S5, sandloom_core: Any, ignore_case: bool
) -> None:
    op = OPS[ignore_case]
    core = sandloom_core.runtime_view()
    with bind_tenant(A):
        for grp in (1, 2):
            for status in STATUS_CASES:
                for starts in PREFIX_CASES:
                    want = _reference(grp, status, starts, ignore_case)
                    key = {"grp": grp, "status": status, "starts": starts}
                    for limit in (1, 3, 100):
                        got = await _walk(lambda: Reads(core), op, limit, **key)
                        assert got == want, (ignore_case, key, limit)
                    async with read_scope(core, "opt") as shared:
                        got = await _walk(lambda s=shared: s, op, 3, **key)
                    assert got == want, ("scope", key)
    # sanity: the cases are not vacuous
    assert len(_reference(1, None, "dev", True)) > len(
        _reference(1, None, "dev", False)
    )
    assert _reference(1, None, "a_", True) and _reference(1, None, "100%", True)


@BOTH
async def test_unit_of_work_reads_apply_the_same_filters(
    opt: S5, sandloom_core: Any
) -> None:
    op = OPS[True]
    with bind_tenant(A):
        async with db_session() as db:
            for status in STATUS_CASES:
                for starts in ("DEV", "a_", None):
                    got = await _walk(
                        lambda d=db: session_reads(d),
                        op,
                        4,
                        grp=1,
                        status=status,
                        starts=starts,
                    )
                    assert got == _reference(1, status, starts, True), (status, starts)


@BOTH
async def test_a_cursor_is_bound_to_its_filters(opt: S5, sandloom_core: Any) -> None:
    op = OPS[True]
    core = sandloom_core.runtime_view()
    with bind_tenant(A):
        page = await Reads(core).page(op, limit=2, grp=1, status="open", starts=None)
        assert page.next_cursor is not None
        for other in (
            {"status": None, "starts": None},
            {"status": "closed", "starts": None},
            {"status": "open", "starts": ""},
            {"status": "open", "starts": "dev"},
        ):
            with pytest.raises(InvalidCursor):
                await Reads(core).page(
                    op, limit=2, cursor=page.next_cursor, grp=1, **other
                )
        nxt = await Reads(core).page(
            op, limit=2, cursor=page.next_cursor, grp=1, status="open", starts=None
        )
        assert nxt.items == _reference(1, "open", None, True)[2:4]
    with pytest.raises(TypeError):  # every key is passed (None = absent)
        with bind_tenant(A):
            await Reads(core).page(op, limit=2, grp=1)


@BOTH
async def test_each_combination_is_its_own_statement(opt: S5, sandloom_core: Any) -> None:
    op = OPS[True]
    plan = plan_for(sandloom_core.runtime_view(), op)
    want = "optimized" if opt.variant == "planopt" else "general"
    assert plan.executor == want, plan.reason
    c = plan.compiled
    forms = {}
    for status in (None, "open"):
        for starts in (None, "x"):
            form, values, names = c.bind([1, status, starts])
            forms[(status, starts)] = form
            assert list(names) == ["grp", *(["status"] if status else [])] + (
                ["starts"] if starts else []
            )
            if starts:
                assert values[-1] == "x%"
    assert len({id(f) for f in forms.values()}) == 4
    assert forms[(None, None)] is c  # no optional filter: the base forms
    sql = forms[("open", "x")].sql
    assert sql is not None  # generated in both modes
    assert "e.status = $2 AND e.name ILIKE $3" in sql.replace('"', ""), sql
    assert "IS NULL" not in sql
    assert "tenant_id" in sql
    general = str(forms[("open", "x")].stmt)
    assert "ILIKE" in general.upper() or "lower(" in general, general
