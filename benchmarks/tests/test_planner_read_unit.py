# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Execution planner: the scope-wide READ UNIT of the general executor.

A ``PlannedReads`` route (or a ``read_scope``) runs its general-executor reads
in ONE read-only transaction, opened by the first such read and ended by the
scope, so a route with two declared reads costs what the same route on a
``DbSession`` costs (one checkout, one BEGIN, one tenant GUC) instead of two
transactions. Consecutive optimized reads also share one read-only transaction.

Same environment as the S5 suite: non-superuser role, FORCEd RLS, a pool of
ONE connection through the counting proxy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from test_planner_executors import _clean
from test_s5_secure_read import (  # noqa: F401 - fixtures
    A_ID,
    B_ID,
    S5,
    A,
    B,
    admin,
    headers,
    pg_proxy,
    s5,
)

from sandloom.core.planner import ReadOperation, plan_for, read_scope
from sandloom.core.sql_alchemy.planner_adapter.forms import LeaseError
from sandloom.core.tenant import bind_tenant
from benchsuite.bench.operations import get_tenant_item
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
A2 = A_ID + 1  # the /s5plan2 partner row of A_ID (tenant A)


def _adapter(sandloom_core: Any) -> Any:
    return sandloom_core.database("default").execution_adapter()


class _Computed(BaseModel):
    id: int
    label: str


def _general_only(monkeypatch: pytest.MonkeyPatch) -> ReadOperation[_Computed]:
    """An operation the optimized executor declines (a non-column DTO field)."""
    monkeypatch.setattr(
        BenchTenantItem, "label", property(lambda o: f"#{o.id}"), raising=False
    )
    return ReadOperation(entity=BenchTenantItem, result=_Computed)


# --------------------------------------------------------------------------- #
# Round trips: the two-read route on the general executor == its ORM twin
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("s5", ["plan"], indirect=True)
async def test_two_general_reads_share_one_transaction(s5: S5) -> None:
    h = headers()
    pid = await _clean(s5)
    s5.proxy.reset()
    planned = await s5.get(f"/s5plan2/{A_ID}", headers=h)
    assert planned.status_code == 200, planned.text
    planned_rt = s5.kinds()
    orm = await s5.get(f"/s5two/{A_ID}", headers=h)
    assert orm.status_code == 200, orm.text
    orm_rt = s5.kinds()
    assert planned.json() == orm.json()
    # one transaction for both reads; the ORM UoW's trace, ending in ROLLBACK
    # (a read-only transaction has nothing to commit)
    assert planned_rt.count("SELECT") == 2, planned_rt
    assert planned_rt[:-1] == orm_rt[:-1], (planned_rt, orm_rt)
    assert (planned_rt[-1], orm_rt[-1]) == ("ROLLBACK", "COMMIT")
    await _clean(s5, pid=pid, same=True)


@pytest.mark.parametrize("s5", ["planopt"], indirect=True)
async def test_two_optimized_reads_share_one_read_only_transaction(s5: S5) -> None:
    pid = await _clean(s5)
    s5.proxy.reset()
    resp = await s5.get(f"/s5plan2/{A_ID}", headers=headers())
    assert resp.status_code == 200, resp.text
    assert s5.kinds() == ["BEGIN+SET_CONFIG", "SELECT", "SELECT", "COMMIT"]
    await _clean(s5, pid=pid, same=True)


@BOTH
async def test_not_found_after_successful_reads_returns_the_connection(
    s5: S5,
) -> None:
    """A handler error after the reads (a plain 404) ends the unit normally:
    the connection goes back to the pool, NOT invalidated."""
    pid = await _clean(s5)
    resp = await s5.get(f"/s5plan2/{B_ID}", headers=headers())  # B's row: unseen
    assert resp.status_code == 404
    await _clean(s5, pid=pid, same=True)


# --------------------------------------------------------------------------- #
# One connection per scope (pool of one: a second checkout would deadlock)
# --------------------------------------------------------------------------- #
@BOTH
async def test_optimized_then_general_read_releases_before_switching(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = _general_only(monkeypatch)
    pid = await _clean(s5)

    async def scope() -> None:
        async with read_scope(sandloom_core.runtime_view(), "t") as reads:
            with bind_tenant(A):
                assert (await reads.one(get_tenant_item, id=A_ID)).id == A_ID
                assert (await reads.one(op, id=A2)).id == A2
                assert (await reads.one(get_tenant_item, id=B_ID)) is None

    await asyncio.wait_for(scope(), 5)
    await _clean(s5, pid=pid, same=True)


@BOTH
async def test_optimized_scope_switches_tenant_without_leaking_rows(
    s5: S5, sandloom_core: Any
) -> None:
    pid = await _clean(s5)
    async with read_scope(sandloom_core.runtime_view(), "t") as reads:
        for tenant, own, foreign in ((A, A_ID, B_ID), (B, B_ID, A_ID), (A, A_ID, B_ID)):
            with bind_tenant(tenant):
                assert (await reads.one(get_tenant_item, id=own)).id == own
                assert (await reads.one(get_tenant_item, id=foreign)) is None
    await _clean(s5, pid=pid, same=True)


@BOTH
async def test_general_then_optimizable_read_reuse_the_unit(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = _general_only(monkeypatch)
    core = sandloom_core.runtime_view()
    assert plan_for(core, op).executor == "general"
    pid = await _clean(s5)

    async def scope() -> tuple[Any, Any, Any]:
        async with read_scope(core, "t") as reads:
            with bind_tenant(A):
                first = await reads.one(op, id=A_ID)
                # optimized plan in planopt mode: must run IN the open unit
                second = await reads.one(get_tenant_item, id=A2)
                third = await reads.one(get_tenant_item, id=B_ID)
            return first, second, third

    first, second, third = await asyncio.wait_for(scope(), 5)
    assert first == _Computed(id=A_ID, label=f"#{A_ID}")
    assert second is not None and second.id == A2
    assert third is None  # tenant predicate + RLS inside the shared unit
    await _clean(s5, pid=pid, same=True)


@BOTH
async def test_tenant_switch_inside_one_unit(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit re-binds the tenant per read: predicate AND the RLS GUC."""
    op = _general_only(monkeypatch)
    core = sandloom_core.runtime_view()
    pid = await _clean(s5)
    async with read_scope(core, "t") as reads:
        with bind_tenant(A):
            assert (await reads.one(op, id=A_ID)) is not None
        with bind_tenant(B):
            assert (await reads.one(op, id=A_ID)) is None
            assert (await reads.one(get_tenant_item, id=A_ID)) is None
            assert (await reads.one(get_tenant_item, id=B_ID)).id == B_ID
        with bind_tenant(A):
            assert (await reads.one(get_tenant_item, id=B_ID)) is None
            assert (await reads.one(get_tenant_item, id=A_ID)).id == A_ID
    await _clean(s5, pid=pid, same=True)


# --------------------------------------------------------------------------- #
# Failure paths of the unit: cancellation and errors never pool a transaction
# --------------------------------------------------------------------------- #
@BOTH
@pytest.mark.parametrize("optimized_read", [False, True])
@pytest.mark.parametrize("phase", ["leased", "fetched", "ended"])
async def test_cancellation_at_each_phase_of_the_unit(
    s5: S5,
    sandloom_core: Any,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    optimized_read: bool,
) -> None:
    op = get_tenant_item if optimized_read else _general_only(monkeypatch)
    adapter = _adapter(sandloom_core)
    pid = await _clean(s5)
    reached = asyncio.Event()

    async def block(name: str) -> None:
        if name == phase:
            reached.set()
            await asyncio.Event().wait()  # forever: only cancellation ends it

    async def scope() -> None:
        async with read_scope(sandloom_core.runtime_view(), "t") as reads:
            with bind_tenant(A):
                await reads.one(op, id=A_ID)
                await reads.one(op, id=A2)

    adapter.checkpoint = block
    try:
        task = asyncio.ensure_future(scope())
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        adapter.checkpoint = None
    await _clean(s5, pid=pid, same=False)  # terminated + invalidated
    with bind_tenant(B):
        async with read_scope(sandloom_core.runtime_view(), "t") as reads:
            assert (await reads.one(get_tenant_item, id=A_ID)) is None
            assert (await reads.one(get_tenant_item, id=B_ID)).id == B_ID


@BOTH
async def test_error_in_a_read_aborts_the_unit_for_the_rest_of_the_scope(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = _general_only(monkeypatch)
    adapter = _adapter(sandloom_core)
    pid = await _clean(s5)

    async def boom(name: str) -> None:
        if name == "fetched":
            raise RuntimeError("boom")

    async with read_scope(sandloom_core.runtime_view(), "t") as reads:
        with bind_tenant(A):
            adapter.checkpoint = boom
            try:
                with pytest.raises(RuntimeError, match="boom"):
                    await reads.one(op, id=A_ID)
            finally:
                adapter.checkpoint = None
            # the unit is gone: no silent new transaction inside this scope
            with pytest.raises(LeaseError):
                await reads.one(op, id=A2)
    await _clean(s5, pid=pid, same=False)


@BOTH
async def test_server_error_in_the_unit_invalidates(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = _general_only(monkeypatch)
    pid = await _clean(s5)
    async with read_scope(sandloom_core.runtime_view(), "t") as reads:
        with bind_tenant(A):
            assert (await reads.one(op, id=A_ID)) is not None
            await s5.admin.execute("SELECT 1")  # the admin side stays usable
            with pytest.raises(Exception):  # noqa: B017,PT011 - any driver error
                await reads.one(op, id="not-an-int")  # type: ignore[arg-type]
    await _clean(s5, pid=pid, same=False)
