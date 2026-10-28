# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Execution planner (PROTOTYPE): executor-specific tests on PostgreSQL + RLS.

The S5 suite (``test_s5_secure_read.py``, variants ``plan`` / ``planopt``)
proves the HTTP contract of ``/s5plan``. This file drives the planner directly
on the same environment (non-superuser role, FORCEd RLS, pool of ONE
connection through the counting proxy) and checks what is specific to each
executor: cancellation at every await, errors (Python and server side) never
pooling an open transaction, tenant switching on one pooled connection, the
read-only guarantees, fail-closed without a tenant, and the plan itself.

Every test runs for BOTH executors (``s5`` fixture, indirect params).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import asyncpg
import pytest
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError
from test_s5_secure_read import (  # noqa: F401 - fixtures
    A_ID,
    B_ID,
    ROLE,
    S5,
    A,
    B,
    admin,
    headers,
    pg_proxy,
    s5,
)

from sandloom.core.di import on_commit, publish_event, record_audit
from sandloom.core.planner import (
    ExecutionContext,
    PlanningError,
    ReadOnlyScopeError,
    ReadOperation,
    execute_one,
    plan_for,
    read_scope,
)
from sandloom.core.config.settings import settings
from sandloom.core.sql_alchemy.planner_adapter.forms import LeaseError
from sandloom.core.config.settings import settings
from sandloom.core.sql_alchemy.planner_adapter.forms import LeaseError
from sandloom.core.tenant import MissingTenantError, bind_tenant, unscoped
from sandloom.core.unit_of_work.effects import AuditUnavailableError
from sandloom.core.unit_of_work.scope import read_only_scope
from benchsuite import common
from benchsuite.bench.api import ItemOut
from benchsuite.bench.operations import get_tenant_item
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
PHASES = {
    "plan": ("leased", "fetched", "ended"),
    "planopt": ("leased", "begun", "fetched", "ended"),
}
#: the same table read by a NON-primary key (general: select(...).where())
by_name = ReadOperation(entity=BenchTenantItem, result=ItemOut, by=("name",))


def _adapter(sandloom_core: Any) -> Any:
    return sandloom_core.database("default").execution_adapter()


async def _read(sandloom_core: Any, tenant: uuid.UUID | None, item_id: int) -> Any:
    core = sandloom_core.runtime_view()
    if tenant is None:
        return await execute_one(core, get_tenant_item, {"id": item_id})
    with bind_tenant(tenant):
        return await execute_one(core, get_tenant_item, {"id": item_id})


async def _clean(s5: S5, *, pid: int | None = None, same: bool | None = None) -> int:
    """The pooled connection is reusable: no tx, no GUC, nothing checked out."""
    new_pid, in_tx, tenant, bypass = await s5.pooled_state()
    assert (in_tx, tenant, bypass) == (False, "", ""), "transaction/GUC leaked"
    assert s5.engine.pool.checkedout() == 0
    assert await s5.idle_in_tx() == 0
    if pid is not None and same is not None:
        assert (new_pid == pid) is same, (pid, new_pid)
    return int(new_pid)


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #
@BOTH
async def test_plan_selects_the_executor_and_holds_no_tenant(
    s5: S5, sandloom_core: Any
) -> None:
    plan = plan_for(sandloom_core.runtime_view(), get_tenant_item)
    want = "optimized" if s5.variant == "planopt" else "general"
    assert plan.executor == want, plan.reason
    assert plan.tenant_scoped is True and plan.fields == ("id", "name", "value")
    sql = plan.compiled.sql
    assert sql == (
        "SELECT id, name, value FROM bench_tenant_items WHERE id = $1 AND tenant_id = $2"
    )
    assert str(A) not in repr(plan) and str(B) not in repr(plan)


@BOTH
async def test_planning_refuses_inconsistent_declarations(
    s5: S5, sandloom_core: Any
) -> None:
    core = sandloom_core.runtime_view()

    class Missing(BaseModel):
        id: int
        nope: str

    with pytest.raises(PlanningError, match="TenantScoped"):
        plan_for(
            core,
            ReadOperation(entity=BenchTenantItem, result=ItemOut, tenant_scoped=False),
        )
    with pytest.raises(PlanningError, match="nope"):
        plan_for(core, ReadOperation(entity=BenchTenantItem, result=Missing))
    with pytest.raises(PlanningError, match="not a column"):
        plan_for(core, ReadOperation(entity=BenchTenantItem, result=ItemOut, by=("x",)))


class _Computed(BaseModel):
    id: int
    label: str


@BOTH
async def test_non_column_field_stays_on_the_general_executor(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        BenchTenantItem, "label", property(lambda o: f"#{o.id}"), raising=False
    )
    op = ReadOperation(entity=BenchTenantItem, result=_Computed)
    plan = plan_for(sandloom_core.runtime_view(), op)
    assert plan.executor == "general"
    if s5.variant == "planopt":
        assert "not a plain column" in plan.reason
    with bind_tenant(A):
        out = await execute_one(sandloom_core.runtime_view(), op, {"id": A_ID})
    assert out == _Computed(id=A_ID, label=f"#{A_ID}")


@BOTH
async def test_both_executors_return_the_same_declared_dto(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = []
    for optimized in (False, True):
        monkeypatch.setattr(settings, "planner_optimized_executor", optimized)
        with bind_tenant(A):
            core = sandloom_core.runtime_view()
            results.append(
                (
                    await execute_one(core, get_tenant_item, {"id": A_ID}),
                    await execute_one(core, by_name, {"name": f"titem-{A_ID}"}),
                    await execute_one(core, by_name, {"name": f"titem-{B_ID}"}),
                )
            )
    assert results[0] == results[1]
    assert results[0][0] == ItemOut(id=A_ID, name=f"titem-{A_ID}", value=1)
    assert type(results[0][0]) is ItemOut and results[0][1] == results[0][0]
    assert results[0][2] is None  # B's row by name, tenant A: invisible
    with pytest.raises(TypeError):
        await execute_one(sandloom_core.runtime_view(), get_tenant_item, {"pk": 1})


# --------------------------------------------------------------------------- #
# Fail closed without a tenant (before any I/O)
# --------------------------------------------------------------------------- #
@BOTH
async def test_no_tenant_fails_closed_without_io(s5: S5, sandloom_core: Any) -> None:
    await _read(sandloom_core, A, A_ID)  # warm
    s5.proxy.reset()
    with pytest.raises(MissingTenantError):
        await _read(sandloom_core, None, A_ID)
    with unscoped(), pytest.raises(MissingTenantError):
        await _read(sandloom_core, None, A_ID)  # unscoped() is no way around it
    assert s5.kinds() == []
    assert s5.engine.pool.checkedout() == 0


# --------------------------------------------------------------------------- #
# Pool of ONE: tenant switches on the same connection
# --------------------------------------------------------------------------- #
@BOTH
async def test_pool_of_one_tenant_switch(s5: S5, sandloom_core: Any) -> None:
    pid = await _clean(s5)
    for i in range(10):
        tenant, own, other = (A, A_ID, B_ID) if i % 2 == 0 else (B, B_ID, A_ID)
        assert (await _read(sandloom_core, tenant, own)).id == own
        assert await _read(sandloom_core, tenant, other) is None
        await _clean(s5, pid=pid, same=True)  # success path: never invalidated
    # the general executor's READ ONLY characteristic is reset on return: an
    # ordinary read-write UoW on the SAME connection can still write
    async with s5.engine.connect() as conn:
        raw = await conn.get_raw_connection()
        assert raw.dbapi_connection.readonly is False
    with bind_tenant(A):
        async with s5.engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE bench_tenant_items SET value = value WHERE id = $1", (A_ID,)
            )
    await _clean(s5, pid=pid, same=True)


# --------------------------------------------------------------------------- #
# Cancellation at every await of each executor
# --------------------------------------------------------------------------- #
@BOTH
@pytest.mark.parametrize("phase_index", [0, 1, 2, 3])
async def test_cancellation_at_each_phase(
    s5: S5, sandloom_core: Any, phase_index: int
) -> None:
    phases = PHASES[s5.variant]
    if phase_index >= len(phases):
        pytest.skip(f"{s5.variant} has {len(phases)} phases")
    phase = phases[phase_index]
    adapter = _adapter(sandloom_core)
    pid = await _clean(s5)
    reached = asyncio.Event()

    async def block(name: str) -> None:
        if name == phase:
            reached.set()
            await asyncio.Event().wait()  # forever: only cancellation ends it

    adapter.checkpoint = block
    try:
        task = asyncio.ensure_future(_read(sandloom_core, A, A_ID))
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        adapter.checkpoint = None
    # cancelled anywhere inside the lease -> terminated + invalidated
    await _clean(s5, pid=pid, same=False)
    assert (await _read(sandloom_core, B, A_ID)) is None
    assert (await _read(sandloom_core, B, B_ID)).id == B_ID


@BOTH
async def test_cancellation_while_the_query_is_in_flight(s5: S5, sandloom_core: Any) -> None:
    """The SELECT blocks SERVER-SIDE (table locked); the client gives up."""
    pid = await _clean(s5)
    lock = s5.admin.transaction()
    await lock.start()
    try:
        await s5.admin.execute("LOCK TABLE bench_tenant_items IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_read(sandloom_core, A, A_ID), 0.5)
    finally:
        await lock.rollback()
    await _clean(s5, pid=pid, same=False)
    # the abandoned backend is gone (no waiter left behind on the lock)
    waiting = await s5.admin.fetchval(
        "SELECT count(*) FROM pg_stat_activity WHERE usename = $1 AND wait_event_type = 'Lock'",
        ROLE,
    )
    assert waiting == 0
    assert (await _read(sandloom_core, A, A_ID)).id == A_ID


# --------------------------------------------------------------------------- #
# Errors never pool an open transaction
# --------------------------------------------------------------------------- #
@BOTH
async def test_python_error_inside_the_transaction_invalidates(
    s5: S5, sandloom_core: Any
) -> None:
    adapter = _adapter(sandloom_core)
    pid = await _clean(s5)

    async def boom(name: str) -> None:
        if name == "fetched":  # after the read, the transaction still open
            raise RuntimeError("boom")

    adapter.checkpoint = boom
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await _read(sandloom_core, A, A_ID)
    finally:
        adapter.checkpoint = None
    await _clean(s5, pid=pid, same=False)


@BOTH
async def test_server_error_invalidates(s5: S5, sandloom_core: Any) -> None:
    pid = await _clean(s5)
    await s5.admin.execute(f"REVOKE SELECT ON bench_tenant_items FROM {ROLE}")
    try:
        with pytest.raises((DBAPIError, asyncpg.InsufficientPrivilegeError)):
            await _read(sandloom_core, A, A_ID)
    finally:
        await s5.admin.execute(f"GRANT SELECT ON bench_tenant_items TO {ROLE}")
    await _clean(s5, pid=pid, same=False)
    assert (await _read(sandloom_core, A, A_ID)).id == A_ID


@BOTH
async def test_a_dirty_pooled_connection_is_refused(s5: S5, sandloom_core: Any) -> None:
    async with s5.engine.connect() as conn:  # a raw BEGIN the pool cannot see
        raw = (await conn.get_raw_connection()).driver_connection
        await raw.execute("BEGIN")
    with pytest.raises(LeaseError):
        await _read(sandloom_core, A, A_ID)
    await _clean(s5)


# --------------------------------------------------------------------------- #
# Read-only: the scope refuses the write-path primitives, the DB refuses writes
# --------------------------------------------------------------------------- #
async def test_read_only_scope_refuses_write_primitives() -> None:
    with read_only_scope("test"):
        with pytest.raises(ReadOnlyScopeError):
            await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
        with pytest.raises(ReadOnlyScopeError):
            record_audit("bench.x", resource_type="x", resource_id="1")
        with pytest.raises(ReadOnlyScopeError):
            record_audit("bench.x", resource_type="x", resource_id="1", required=True)
        with pytest.raises(ReadOnlyScopeError):
            await on_commit(lambda: None)
        with read_only_scope("inner"):  # nests
            with pytest.raises(ReadOnlyScopeError, match="test"):
                await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
    # outside the scope the previous behaviour is unchanged
    with pytest.raises(AuditUnavailableError):
        record_audit("bench.x", resource_type="x", resource_id="1", required=True)


@BOTH
async def test_executor_transactions_refuse_writes(s5: S5, sandloom_core: Any) -> None:
    adapter = _adapter(sandloom_core)
    ctx = ExecutionContext(database="default", tenant=A, tenant_scoped=True)
    before = await s5.admin.fetchval("SELECT count(*) FROM bench_tenant_items")
    pid = await _clean(s5)
    with pytest.raises(DBAPIError, match="read-only transaction"):
        async with adapter.session(ctx) as session:
            session.add(BenchTenantItem(id=999_010, name="x", value=1))
            await session.flush()
    pid = await _clean(s5, pid=pid, same=False)
    with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
        async with adapter.raw_transaction(ctx) as raw:
            await raw.execute(
                "UPDATE bench_tenant_items SET value = 2 WHERE id = $1", A_ID
            )
    await _clean(s5, pid=pid, same=False)
    assert await s5.admin.fetchval("SELECT count(*) FROM bench_tenant_items") == before
    assert (
        await s5.admin.fetchval(
            "SELECT value FROM bench_tenant_items WHERE id = $1", A_ID
        )
        == 1
    )


@BOTH
async def test_worker_read_scope(s5: S5, sandloom_core: Any) -> None:
    """Outside HTTP (worker / script): the same handle and guarantees."""
    async with read_scope(sandloom_core.runtime_view(), "worker") as reads:
        with bind_tenant(B):
            assert (await reads.one(get_tenant_item, id=B_ID)).id == B_ID
            with pytest.raises(ReadOnlyScopeError):
                await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
    await _clean(s5)


# --------------------------------------------------------------------------- #
# The universal path pays nothing for the planner's existence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("s5", ["uow"], indirect=True)
async def test_plain_dbsession_routes_run_no_planner_code(
    s5: S5, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route that does not declare PlannedReads executes NO planner code:
    every planner / adapter / lease / read-only-scope entry point is trapped,
    and plain DbSession requests (read, raw SQL read, 404) still succeed."""
    import sandloom.core.planner as planner
    from sandloom.core import di
    from sandloom.core import unit_of_work as uow
    from sandloom.core.unit_of_work import scope as uow_scope
    from sandloom.core.drivers.sqlalchemy_database import SQLAlchemyDatabaseProvider
    from sandloom.core.sql_alchemy import planner_adapter as pa
    from sandloom.core.sql_alchemy.planner_adapter import units as pa_units  # noqa: PLC0415

    hit: list[str] = []

    def trap(name: str) -> Any:
        def _trapped(*_a: Any, **_k: Any) -> Any:
            hit.append(name)
            raise AssertionError(f"planner code on a plain route: {name}")

        return _trapped

    targets = [
        (planner, "_adapter"),
        (planner, "_plan"),
        (planner, "plan_for"),
        (planner, "execution_context"),
        (planner.Reads, "one"),
        (planner.Reads, "__init__"),
        (pa.SQLAlchemyExecutionAdapter, "fetch_one"),
        (pa.SQLAlchemyExecutionAdapter, "compile"),
        (pa.SQLAlchemyExecutionAdapter, "lease"),
        (pa_units._Lease, "__aenter__"),
        (SQLAlchemyDatabaseProvider, "execution_adapter"),
        (uow_scope, "read_only_scope"),
        (di, "_planner_reads"),
    ]
    for owner, name in targets:
        monkeypatch.setattr(owner, name, trap(f"{owner.__name__}.{name}"))
    # the route-level scope marker is never set on a plain route either
    assert uow_scope._read_only_scope.get() is None

    r = await s5.get(f"/s5/{A_ID}", headers=headers(A))
    assert r.status_code == 200, r.text
    assert (await s5.get(f"/s5/{B_ID}", headers=headers(A))).status_code == 404
    r = await s5.get(f"/_p/session/{A_ID},{B_ID}", headers=headers(A))
    assert r.status_code == 200 and r.json() == [A_ID]
    assert hit == []
    # the traps are live: a PLANNED route trips them (so the check above bites)
    assert (await s5.get(f"/s5plan/{A_ID}", headers=headers(A))).status_code == 500
    assert hit
