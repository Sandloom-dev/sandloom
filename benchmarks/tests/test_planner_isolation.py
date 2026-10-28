# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Isolation and locking are part of the read contract, not planner side effects.

* A read-only scope is READ COMMITTED by default (explicit: ``BEGIN ISOLATION
  LEVEL READ COMMITTED READ ONLY`` / the read unit's ``isolation_level``):
  two reads of ONE scope may see different snapshots — tested.
* ``repeatable_read`` (``SnapshotReads`` / ``read_scope(..., isolation=...)``)
  gives the whole scope ONE snapshot, on both executors (every read runs in
  the scope's read unit) — tested. Any other isolation is refused at
  planning; inside a Unit of Work the isolation is the unit's.
* Locking reads are never planned: a declaration with ``lock=`` is refused,
  the read-only transaction refuses ``FOR UPDATE`` even through the executor
  primitives, and an existing locking query in a Unit of Work behaves exactly
  as before (it takes and holds the row lock; a planned read in the same unit
  runs on the same connection).
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest
from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from test_planner_executors import _clean
from test_s5_secure_read import (  # noqa: F401 - fixtures
    A_ID,
    S5,
    A,
    _ctx,
    admin,
    headers,
    pg_proxy,
    s5,
)

from sandloom.core.di import SnapshotReads, TenantScope
from sandloom.core.orm import ReadOperation, session_reads
from sandloom.core.planner import PlanningError, Reads, plan_for, read_scope
from sandloom.core.tenant import bind_tenant
from sandloom.core.unit_of_work import db_session
from benchsuite.bench.api import ItemOut
from benchsuite.bench.operations import get_tenant_item
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)


async def _bump(s5: S5, delta: int = 1) -> None:
    """A concurrent COMMITTED write (another connection)."""
    await s5.admin.execute(
        "UPDATE bench_tenant_items SET value = value + $1 WHERE id = $2", delta, A_ID
    )


@BOTH
async def test_default_scope_is_read_committed(s5: S5, sandloom_core: Any) -> None:
    """Two reads of ONE default scope may see different snapshots."""
    core = sandloom_core.runtime_view()
    pid = await _clean(s5)
    try:
        async with read_scope(core, "t") as reads:
            with bind_tenant(A):
                first = await reads.one(get_tenant_item, id=A_ID)
                await _bump(s5)
                second = await reads.one(get_tenant_item, id=A_ID)
    finally:
        await _bump(s5, -1)
    assert second.value == first.value + 1  # the committed change is visible
    await _clean(s5, pid=pid, same=True)


@BOTH
async def test_repeatable_read_scope_is_one_snapshot(s5: S5, sandloom_core: Any) -> None:
    core = sandloom_core.runtime_view()
    pid = await _clean(s5)
    try:
        async with read_scope(core, "t", isolation="repeatable_read") as reads:
            with bind_tenant(A):
                first = await reads.one(get_tenant_item, id=A_ID)
                await _bump(s5)
                second = await reads.one(get_tenant_item, id=A_ID)
    finally:
        await _bump(s5, -1)
    assert second == first  # one snapshot for the whole scope
    await _clean(s5, pid=pid, same=True)  # isolation reset on return


@BOTH
async def test_snapshot_route(s5: S5, sandloom_app: Any) -> None:
    r = APIRouter()

    @r.get("/_iso/{item_id}")
    async def iso(item_id: int, reads: SnapshotReads, t: Any = TenantScope()) -> Any:
        first = await reads.one(get_tenant_item, id=item_id)
        await _bump(s5)
        second = await reads.one(get_tenant_item, id=item_id)
        return {"same": first == second}

    before = list(sandloom_app.router.routes)
    sandloom_app.include_router(r)
    try:
        resp = await s5.get(f"/_iso/{A_ID}", headers=headers())
    finally:
        sandloom_app.router.routes[:] = before
        await _bump(s5, -1)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"same": True}
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan"], indirect=True)
async def test_unsupported_isolation_is_refused(s5: S5, sandloom_core: Any) -> None:
    core = sandloom_core.runtime_view()
    with pytest.raises(PlanningError, match="isolation"):
        Reads(core, shared=True, isolation="serializable")  # type: ignore[arg-type]
    with pytest.raises(PlanningError, match="SCOPE"):
        Reads(core, isolation="repeatable_read")  # one-shot handle: no scope
    with pytest.raises(PlanningError, match="Unit of Work"):
        Reads(core, object(), shared=True, isolation="repeatable_read")


# --------------------------------------------------------------------------- #
# Locking reads
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("s5", ["plan"], indirect=True)
async def test_locking_declarations_are_refused(s5: S5) -> None:
    for lock in ("update", "share", "update nowait", "update skip locked"):
        with pytest.raises(PlanningError, match="locking read"):
            ReadOperation(entity=BenchTenantItem, result=ItemOut, lock=lock)


@BOTH
async def test_read_only_executor_transactions_refuse_for_update(
    s5: S5, sandloom_core: Any
) -> None:
    """Even through the executor primitives, FOR UPDATE cannot run in a
    planner transaction (READ ONLY): refused, the connection invalidated."""
    adapter = sandloom_core.database("default").execution_adapter()
    pid = await _clean(s5)
    with pytest.raises(DBAPIError, match="read-only"):
        async with adapter.session(_ctx(A)) as session:
            await session.execute(
                select(BenchTenantItem)
                .where(BenchTenantItem.id == A_ID)
                .with_for_update()
            )
    await _clean(s5, pid=pid, same=False)


@BOTH
async def test_existing_locking_query_in_a_unit_of_work_is_unchanged(
    s5: S5,
) -> None:
    """A UoW's SELECT ... FOR UPDATE takes and holds the lock (a concurrent
    NOWAIT fails); a planned read in the same unit uses the same connection
    (pool of one: no second checkout) and sees the locked row."""

    async def nowait() -> None:
        await s5.admin.execute(
            "SELECT 1 FROM bench_tenant_items WHERE id = $1 FOR UPDATE NOWAIT", A_ID
        )

    with bind_tenant(A):
        async with db_session() as db:
            locked = (
                await db.execute(
                    select(BenchTenantItem)
                    .where(BenchTenantItem.id == A_ID)
                    .with_for_update(nowait=True)
                )
            ).scalar_one()
            planned = await asyncio.wait_for(
                session_reads(db).one(get_tenant_item, id=A_ID), 5
            )
            assert planned is not None and planned.value == locked.value
            tx = s5.admin.transaction()
            await tx.start()
            try:
                with pytest.raises(asyncpg.LockNotAvailableError):
                    await nowait()
            finally:
                await tx.rollback()
    await nowait()  # released at the unit's end
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan"], indirect=True)
@pytest.mark.parametrize("server_default_is_rc", [True, False])
async def test_general_executor_transaction_isolation_is_explicit(
    s5: S5, sandloom_core: Any, server_default_is_rc: bool
) -> None:
    """The general executor's transaction is READ COMMITTED READ ONLY, whether
    the server default is (fast path) or is not (pinned per checkout)."""
    from sqlalchemy import text

    adapter = sandloom_core.database("default").execution_adapter()
    saved = adapter._rc_default
    adapter._rc_default = server_default_is_rc
    try:
        async with adapter.session(_ctx(A)) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT current_setting('transaction_isolation'), "
                        "current_setting('transaction_read_only')"
                    )
                )
            ).one()
    finally:
        adapter._rc_default = saved
    assert tuple(row) == ("read committed", "on")
    ru = adapter.read_unit("REPEATABLE READ")
    with bind_tenant(A):
        await ru.fetch(
            plan_for(sandloom_core.runtime_view(), get_tenant_item),
            _ctx(A),
            [A_ID],
        )
        level = (
            await ru._session.execute(
                text("SELECT current_setting('transaction_isolation')")
            )
        ).scalar_one()
        await ru.close()
    assert level == "repeatable read"
    await _clean(s5)
