# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""``ListOperation``: tenant-scoped keyset pages with an opaque cursor.

S5 environment (FORCEd RLS, NOBYPASSRLS role, pool of one), both executors:
tenant isolation across every page, ordering stability (no row twice, none
skipped, also with rows inserted between pages), the cursor is tamper-evident
and bound to the operation, its filter arguments and the bound tenant (a
foreign / forged / garbage cursor is a 400 BEFORE any I/O), the limit is
mandatory and capped, the general and optimized executors return the same
rows in the same order, unplannable declarations fail at planning, and inside
a Unit of Work a list sees the unit's own unflushed writes.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
from pydantic import BaseModel
from test_planner_executors import _clean
from test_s5_secure_read import (  # noqa: F401 - fixtures
    S5,
    A,
    B,
    admin,
    headers,
    pg_proxy,
    s5,
)

from sandloom.core.orm import (
    InvalidCursor,
    ListOperation,
    PageLimitError,
    make_cursor,
    session_reads,
)
from sandloom.core.planner import Reads, plan_for, read_scope
from sandloom.core.config.settings import settings
from sandloom.core.tenant import MissingTenantError, bind_tenant
from sandloom.core.unit_of_work import db_session
from benchsuite import common
from benchsuite.bench.api import ItemOut
from benchsuite.bench.operations import list_tenant_items
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
PLAN = pytest.mark.parametrize("s5", ["plan"], indirect=True)
N = common.S5_ROWS_PER_TENANT

#: a filtered list: the rows of one ``value``, newest id first
by_value_desc = ListOperation(
    entity=BenchTenantItem,
    result=ItemOut,
    by=("value",),
    order_by=("id",),
    descending=True,
    require_index=False,  # no (tenant_id, value, id) index: pages sort (warned)
    name="test.items.by_value_desc",
)


async def _all_pages(
    sandloom_core: Any, op: ListOperation[Any], *, limit: int, scoped: bool, **key: Any
) -> tuple[list[int], int]:
    core = sandloom_core.runtime_view()
    ids: list[int] = []
    pages = 0
    cursor = None
    async with read_scope(core, "t") as shared:
        while True:
            reads = shared if scoped else Reads(core)
            page = await reads.page(op, limit=limit, cursor=cursor, **key)
            pages += 1
            ids.extend(item.id for item in page.items)
            assert len(page.items) <= limit
            cursor = page.next_cursor
            if cursor is None:
                return ids, pages


async def _tenant_ids(s5: S5, tenant: uuid.UUID, where: str = "") -> list[int]:
    rows = await s5.admin.fetch(
        f"SELECT id FROM bench_tenant_items WHERE tenant_id = $1 {where} ORDER BY id",
        tenant,
    )
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Isolation, order, stability
# --------------------------------------------------------------------------- #
@BOTH
@pytest.mark.parametrize("scoped", [False, True])
async def test_pages_cover_the_tenant_exactly_once_in_order(
    s5: S5, sandloom_core: Any, scoped: bool
) -> None:
    pid = await _clean(s5)
    for tenant in (A, B):
        with bind_tenant(tenant):
            ids, pages = await _all_pages(
                sandloom_core, list_tenant_items, limit=200, scoped=scoped
            )
        assert ids == await _tenant_ids(s5, tenant)  # every row once, in order
        assert pages == -(-N // 200)  # limit+1 probe: no empty trailing page
    await _clean(s5, pid=pid, same=True)


@BOTH
async def test_rows_inserted_between_pages_never_duplicate_or_skip(
    s5: S5, sandloom_core: Any
) -> None:
    core = sandloom_core.runtime_view()
    with bind_tenant(A):
        first = await Reads(core).page(list_tenant_items, limit=100)
        before = [i.id for i in first.items]
        # one row BEFORE the cursor position, one after the end
        await s5.admin.execute(
            "INSERT INTO bench_tenant_items (id, tenant_id, name, value) VALUES "
            "(0, $1, 'early', 1), ($2, $1, 'late', 1)",
            A,
            10 * N + 1,
        )
        try:
            ids = list(before)
            cursor = first.next_cursor
            while cursor is not None:
                page = await Reads(core).page(
                    list_tenant_items, limit=200, cursor=cursor
                )
                ids.extend(i.id for i in page.items)
                cursor = page.next_cursor
        finally:
            await s5.admin.execute(
                "DELETE FROM bench_tenant_items WHERE id IN (0, $1)", 10 * N + 1
            )
    assert len(ids) == len(set(ids))  # never twice
    assert 0 not in ids  # inserted behind the cursor: not visited
    assert ids[-1] == 10 * N + 1  # inserted ahead: visited once
    assert ids[:-1] == [*range(1, N + 1)]  # nothing pre-existing skipped


@BOTH
async def test_descending_filtered_list(s5: S5, sandloom_core: Any) -> None:
    value = -5
    rows = [(10 * N + i, A if i % 3 else B) for i in range(30)]
    await s5.admin.executemany(
        "INSERT INTO bench_tenant_items (id, tenant_id, name, value) "
        "VALUES ($1, $2, 'v', -5)",
        rows,
    )
    try:
        with bind_tenant(A):
            ids, pages = await _all_pages(
                sandloom_core, by_value_desc, limit=7, scoped=True, value=value
            )
        expected = await _tenant_ids(s5, A, f"AND value = {value}")
    finally:
        await s5.admin.execute("DELETE FROM bench_tenant_items WHERE value = -5")
    assert ids == expected[::-1] and len(ids) == 20 and pages == 3


@PLAN
async def test_general_and_optimized_return_identical_pages(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = []
    for optimized in (False, True):
        monkeypatch.setattr(settings, "planner_optimized_executor", optimized)
        plan = plan_for(sandloom_core.runtime_view(), list_tenant_items)
        assert plan.executor == ("optimized" if optimized else "general")
        with bind_tenant(B):
            results.append(
                await _all_pages(sandloom_core, list_tenant_items, limit=150, scoped=False)
            )
    assert results[0] == results[1]


@pytest.mark.parametrize("s5", ["planopt"], indirect=True)
async def test_optimized_page_is_one_read_only_transaction(s5: S5) -> None:
    await s5.get("/s5list?limit=1", headers=headers())  # connection warm-up
    s5.proxy.reset()
    resp = await s5.get("/s5list?limit=3", headers=headers())
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()["items"]] == [1, 2, 3]
    assert s5.kinds() == ["BEGIN+SET_CONFIG", "SELECT", "COMMIT"]


# --------------------------------------------------------------------------- #
# The cursor: opaque, tamper-evident, bound to op + filters + tenant
# --------------------------------------------------------------------------- #
def _forge(cursor: str, position: list[Any]) -> str:
    """Same MAC, another position (what a client could try)."""
    _, _, mac = cursor.partition(".")
    payload = base64.urlsafe_b64encode(json.dumps(position).encode()).rstrip(b"=")
    return f"{payload.decode()}.{mac}"


@BOTH
async def test_tampered_or_foreign_cursors_are_refused_before_any_io(
    s5: S5, sandloom_core: Any
) -> None:
    core = sandloom_core.runtime_view()
    with bind_tenant(A):
        good = (await Reads(core).page(list_tenant_items, limit=10)).next_cursor
        assert good is not None
        value_cursor = (
            await Reads(core).page(by_value_desc, limit=1, value=1)
        ).next_cursor
    bad = [
        _forge(good, [B.int]),  # moved position, old MAC
        good[:-2] + ("AA" if not good.endswith("AA") else "BB"),  # MAC flipped
        "garbage",
        "",
        "....",
        good + "x",
    ]
    s5.proxy.reset()
    with bind_tenant(A):
        for cursor in bad:
            with pytest.raises(InvalidCursor):
                await Reads(core).page(list_tenant_items, limit=10, cursor=cursor)
        # another operation's cursor
        if value_cursor is not None:
            with pytest.raises(InvalidCursor):
                await Reads(core).page(list_tenant_items, limit=10, cursor=value_cursor)
            # same operation, other filter arguments
            with pytest.raises(InvalidCursor):
                await Reads(core).page(
                    by_value_desc, limit=1, cursor=value_cursor, value=2
                )
    with bind_tenant(B), pytest.raises(InvalidCursor):  # another tenant
        await Reads(core).page(list_tenant_items, limit=10, cursor=good)
    assert s5.kinds() == []  # refused before any I/O
    await _clean(s5)


@BOTH
async def test_http_cursor_and_limit_errors_are_400(s5: S5) -> None:
    h = headers()
    first = await s5.get("/s5list?limit=2", headers=h)
    assert first.status_code == 200
    cursor = first.json()["next"]
    nxt = await s5.get(f"/s5list?limit=2&cursor={cursor}", headers=h)
    assert [i["id"] for i in nxt.json()["items"]] == [3, 4]
    for query in (
        f"limit=2&cursor={cursor[:-1]}",  # tampered
        "limit=2&cursor=nope",
        "limit=0",
        "limit=201",  # above the declaration's max_limit=200
    ):
        resp = await s5.get(f"/s5list?{query}", headers=h)
        assert resp.status_code == 400, (query, resp.text)
    # another tenant's cursor
    resp = await s5.get(f"/s5list?limit=2&cursor={cursor}", headers=headers(B))
    assert resp.status_code == 400
    await _clean(s5)


@PLAN
async def test_limit_is_mandatory_and_capped(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = sandloom_core.runtime_view()
    with bind_tenant(A):
        for limit in (0, -1, 201, True, "3", 2.0):
            with pytest.raises(PageLimitError):
                await Reads(core).page(list_tenant_items, limit=limit)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await Reads(core).page(list_tenant_items)  # type: ignore[call-arg]
        monkeypatch.setattr(settings, "planner_list_max_limit", 10)
        assert list_tenant_items.page_cap() == 10  # framework cap wins
        with pytest.raises(PageLimitError):
            await Reads(core).page(list_tenant_items, limit=11)
        assert len((await Reads(core).page(list_tenant_items, limit=10)).items) == 10


@PLAN
async def test_no_tenant_no_io(s5: S5, sandloom_core: Any) -> None:
    s5.proxy.reset()
    with pytest.raises(MissingTenantError):
        await Reads(sandloom_core.runtime_view()).page(list_tenant_items, limit=5)
    assert s5.kinds() == []


@PLAN
async def test_make_cursor_starts_after_the_position(s5: S5, sandloom_core: Any) -> None:
    with bind_tenant(A):
        cursor = make_cursor(list_tenant_items, (41,))
        page = await Reads(sandloom_core.runtime_view()).page(
            list_tenant_items, limit=2, cursor=cursor
        )
    assert [i.id for i in page.items] == [42, 43]


# --------------------------------------------------------------------------- #
# Planning and the Unit of Work
# --------------------------------------------------------------------------- #
@PLAN
async def test_declarations_that_could_sort_or_duplicate_are_refused(
    s5: S5, sandloom_core: Any
) -> None:
    from sandloom.core.planner import PlanningError

    core = sandloom_core.runtime_view()
    with pytest.raises(PlanningError, match="index"):
        plan_for(
            core,
            ListOperation(entity=BenchTenantItem, result=ItemOut, order_by=("value",)),
        )
    with pytest.raises(PlanningError, match="tenant"):
        plan_for(
            core,
            ListOperation(entity=BenchTenantItem, result=ItemOut, tenant_scoped=False),
        )


class _Shout(BaseModel):
    id: int
    shout: str


@PLAN
async def test_non_column_field_lists_on_the_general_executor(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "planner_optimized_executor", True)
    monkeypatch.setattr(
        BenchTenantItem, "shout", property(lambda o: o.name.upper()), raising=False
    )
    op = ListOperation(entity=BenchTenantItem, result=_Shout, name="t.shout")
    plan = plan_for(sandloom_core.runtime_view(), op)
    assert plan.executor == "general" and "not a plain column" in plan.reason
    with bind_tenant(A):
        page = await Reads(sandloom_core.runtime_view()).page(op, limit=2)
    assert [p.shout for p in page.items] == ["TITEM-1", "TITEM-2"]


@BOTH
async def test_list_inside_a_unit_of_work_sees_unflushed_writes(s5: S5) -> None:
    class _Undo(Exception):
        pass

    with bind_tenant(A), pytest.raises(_Undo):
        async with db_session() as db:
            db.add(BenchTenantItem(id=10 * N + 7, name="new", value=-77))
            page = await session_reads(db).page(by_value_desc, limit=5, value=-77)
            assert [i.id for i in page.items] == [10 * N + 7]
            raise _Undo
    assert not await s5.admin.fetchval(
        "SELECT count(*) FROM bench_tenant_items WHERE value = -77"
    )


# --------------------------------------------------------------------------- #
# Cursor semantics: tamper-proof, NOT a snapshot
# --------------------------------------------------------------------------- #
@BOTH
async def test_rows_deleted_between_pages_are_gone_and_nothing_else_moves(
    s5: S5, sandloom_core: Any
) -> None:
    """Keyset on (order columns, pk): a row deleted ahead of the cursor is
    simply not returned; every other row (whose sort key does not change)
    appears exactly once. Pages are not a snapshot."""
    core = sandloom_core.runtime_view()
    victim = 150
    await s5.admin.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _victim AS "
        "SELECT * FROM bench_tenant_items WHERE id = $1",
        victim,
    )
    with bind_tenant(A):
        first = await Reads(core).page(list_tenant_items, limit=100)
        await s5.admin.execute("DELETE FROM bench_tenant_items WHERE id = $1", victim)
        try:
            ids = [i.id for i in first.items]
            cursor = first.next_cursor
            while cursor is not None:
                page = await Reads(core).page(
                    list_tenant_items, limit=200, cursor=cursor
                )
                ids.extend(i.id for i in page.items)
                cursor = page.next_cursor
        finally:
            await s5.admin.execute(
                "INSERT INTO bench_tenant_items SELECT * FROM _victim"
            )
            await s5.admin.execute("DROP TABLE _victim")
    assert ids == [i for i in range(1, N + 1) if i != victim]


# --------------------------------------------------------------------------- #
# Index efficiency: planning requires a covering index; EXPLAIN proves it
# --------------------------------------------------------------------------- #
@PLAN
async def test_a_list_without_a_covering_index_is_refused_or_warned(
    s5: S5, sandloom_core: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from sandloom.core.planner import PlanningError

    core = sandloom_core.runtime_view()
    with pytest.raises(PlanningError, match="leading columns"):
        plan_for(
            core,
            ListOperation(
                entity=BenchTenantItem, result=ItemOut, by=("value",), name="t.nv"
            ),
        )
    with caplog.at_level("WARNING", logger="sandloom.core.planner"):
        plan = plan_for(core, by_value_desc)
    assert "no covering index" in plan.reason
    assert any("no covering index" in r.getMessage() for r in caplog.records)


async def _explain(s5: S5, sql: str, args: tuple[Any, ...]) -> str:
    raw = await s5.admin.fetchval(f"EXPLAIN (FORMAT JSON) {sql}", *args)
    return json.dumps(json.loads(raw) if isinstance(raw, str) else raw)


@PLAN
async def test_list_pages_are_index_range_scans_explain(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXPLAIN of the generated statements on realistic data (10k items;
    20k members in 20 organizations): index scans, no Sort node, no Seq Scan."""
    from sandloom_tenancy.services.operations import members_of_org

    monkeypatch.setattr(settings, "planner_optimized_executor", True)
    core = sandloom_core.runtime_view()
    # bench: tenant items by id (the PK)
    plan = plan_for(core, list_tenant_items)
    c = plan.compiled
    for sql, after in ((c.sql, None), (c.sql_after, (2500,))):
        text_plan = await _explain(s5, sql, c.sql_args((), A, after, 51))
        assert "Index" in text_plan and '"Sort"' not in text_plan, text_plan
        assert "Seq Scan" not in text_plan, text_plan
    # tenancy members: 20 orgs x 1000 members, analysed; a 25-row page
    orgs = [uuid.uuid4() for _ in range(20)]
    await s5.admin.executemany(
        "INSERT INTO tenancy_organizations (id, name, slug, owner_id, is_active, "
        "created_at, updated_at) VALUES ($1, 'o', $2, $1, true, now(), now())",
        [(o, f"explain-{o.hex[:12]}") for o in orgs],
    )
    await s5.admin.executemany(
        "INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)"
        " VALUES ($1, $2, $3, 'member', now() - make_interval(secs => $4))",
        [(uuid.uuid4(), o, uuid.uuid4(), float(i)) for o in orgs for i in range(100)],
    )
    await s5.admin.execute("ANALYZE tenancy_members; ANALYZE tenancy_organizations")
    plan = plan_for(core, members_of_org)
    c = plan.compiled
    pos = await s5.admin.fetchrow(
        "SELECT created_at, id FROM tenancy_members WHERE organization_id = $1 "
        "ORDER BY created_at, id OFFSET 40 LIMIT 1",
        orgs[7],
    )
    for sql, after in ((c.sql, None), (c.sql_after, (pos[0], pos[1]))):
        text_plan = await _explain(s5, sql, c.sql_args((orgs[7],), None, after, 26))
        assert "ix_tenancy_members_org_created" in text_plan, text_plan
        assert "Seq Scan" not in text_plan, text_plan
        if after is None:  # the first page: an ordered index scan, no sort
            assert '"Sort"' not in text_plan, text_plan
        else:  # the keyset bound is an INDEX condition (a range scan); the
            # planner may still sort the bounded candidates if it estimates few
            assert "ROW(created_at, id) >" in text_plan.split('"Index Cond"')[1]
