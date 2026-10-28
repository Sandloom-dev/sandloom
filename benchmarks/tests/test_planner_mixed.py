# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Execution planner: planned reads INSIDE a read-write Unit of Work (mixed).

Contract under test (docs/design/execution-planner.md, "Mixed operations"):

* a route that declares ``DbSession`` (it writes) runs its declared reads
  through ``UowReads``: the GENERAL executor ON the UoW's session — one
  connection, one transaction, one tenant GUC; read-your-own-writes (ORM
  autoflush + identity map); the UoW's outbox / audit / on_commit semantics
  unchanged;
* a read-only route (``PlannedReads``) keeps its own READ ONLY transactions
  and still refuses the write-path primitives; declaring BOTH is refused;
* a mixed route is indistinguishable from the same route written with plain
  ORM reads: identical SQL trace, identical outbox / audit rows, identical
  on_commit behaviour.

Runs on the S5 environment (non-superuser role, FORCEd RLS, pool of ONE,
counting proxy) for both planner modes (``plan`` / ``planopt``): inside a UoW
the optimized mode must make no difference.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import APIRouter, HTTPException
from test_s5_secure_read import (  # noqa: F401 - fixtures
    A_ID,
    B_ID,
    S5,
    A,
    B,
    _write_state,
    admin,
    headers,
    pg_proxy,
    s5,
)

from sandloom.core.di import (
    DbSession,
    PlannedReads,
    TenantScope,
    UowReads,
    on_commit,
    publish_event,
    record_audit,
)
from sandloom.core.orm import session_reads
from sandloom.core.planner import PlanningError
from sandloom.core.tenant import bind_tenant
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.dispatch import drain_post_commit
from benchsuite import common
from benchsuite.bench.api import ItemOut
from benchsuite.bench.operations import get_tenant_item
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
COMMITTED: list[str] = []
LEAKED: list[Any] = []

Read = Callable[[int], Awaitable[ItemOut | None]]


async def _scenario(action: str, new_id: int, db: Any, read: Read) -> Any:
    """ONE body for the mixed and the plain route: only ``read`` differs."""
    if action == "insert_read":  # an unflushed insert, then read it back
        db.add(BenchTenantItem(id=new_id, name=f"n{new_id}", value=7))
        got = await read(new_id)
        await publish_event(common.EVENT_CHANNEL, {"id": new_id, "name": "x"})
        record_audit(
            "bench.mixed", resource_type="item", resource_id=str(new_id), required=True
        )
        await on_commit(lambda: COMMITTED.append(f"commit:{new_id}"))
        return None if got is None else got.model_dump()
    if action == "update_read":  # an unflushed UPDATE of a loaded row
        item = await db.get(BenchTenantItem, A_ID)
        item.value = 42
        got = await read(A_ID)
        return None if got is None else got.model_dump()
    if action == "fail":  # write, read it, then fail: the UoW rolls back BOTH
        db.add(BenchTenantItem(id=new_id, name=f"n{new_id}", value=7))
        assert (await read(new_id)) is not None
        await publish_event(common.EVENT_CHANNEL, {"id": new_id, "name": "x"})
        record_audit(
            "bench.mixed", resource_type="item", resource_id=str(new_id), required=True
        )
        await on_commit(lambda: COMMITTED.append(f"commit:{new_id}"))
        raise HTTPException(409, "fail after the write and the read")
    if action == "switch":  # tenant switch inside one UoW
        db.add(BenchTenantItem(id=new_id, name=f"n{new_id}", value=7))
        own = await read(new_id)
        with bind_tenant(B):
            b_own, b_cross, b_new = await read(B_ID), await read(A_ID), await read(new_id)
        back = await read(A_ID)
        return [
            x is not None for x in (own, b_own, b_cross, b_new, back)
        ]
    if action == "cross":
        return None if (got := await read(B_ID)) is None else got.model_dump()
    raise HTTPException(400, action)


def _router() -> APIRouter:
    r = APIRouter()

    @r.post("/_m/mixed/{action}/{new_id}")
    async def mixed(
        action: str, new_id: int, db: DbSession, reads: UowReads, t: Any = TenantScope()
    ) -> Any:
        assert reads.in_unit_of_work
        return await _scenario(
            action, new_id, db, lambda i: reads.one(get_tenant_item, id=i)
        )

    @r.post("/_m/plain/{action}/{new_id}")
    async def plain(action: str, new_id: int, db: DbSession, t: Any = TenantScope()) -> Any:
        async def read(i: int) -> ItemOut | None:
            item = await db.get(BenchTenantItem, i)
            return None if item is None else ItemOut(id=item.id, name=item.name, value=item.value)

        return await _scenario(action, new_id, db, read)

    @r.post("/_m/session_reads/{new_id}")
    async def via_session(new_id: int, db: DbSession, t: Any = TenantScope()) -> Any:
        reads = session_reads(db)  # the same mode for code holding a session
        db.add(BenchTenantItem(id=new_id, name=f"n{new_id}", value=7))
        return (await reads.one(get_tenant_item, id=new_id)) is not None

    @r.post("/_m/leak")
    async def leak(db: DbSession, reads: UowReads, t: Any = TenantScope()) -> Any:
        LEAKED.append(reads)
        return (await reads.one(get_tenant_item, id=A_ID)) is not None

    @r.post("/_m/use_leaked")
    async def use_leaked(db: DbSession, t: Any = TenantScope()) -> Any:
        return (await LEAKED[-1].one(get_tenant_item, id=A_ID)) is not None

    @r.post("/_m/slow/{new_id}")
    async def slow(
        new_id: int, db: DbSession, reads: UowReads, t: Any = TenantScope()
    ) -> Any:
        db.add(BenchTenantItem(id=new_id, name=f"n{new_id}", value=7))
        await db.flush()
        await reads.one(get_tenant_item, id=A_ID)  # blocks: table locked
        return True

    @r.get("/_m/ro/{action}")
    async def ro(action: str, reads: PlannedReads, t: Any = TenantScope()) -> Any:
        assert not reads.in_unit_of_work
        assert (await reads.one(get_tenant_item, id=A_ID)) is not None
        if action == "event":
            await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
        elif action == "audit":
            record_audit("bench.mixed", resource_type="x", resource_id="1", required=True)
        elif action == "on_commit":
            await on_commit(lambda: COMMITTED.append("ro"))
        return True

    @r.post("/_m/conflict/{new_id}")
    async def conflict(
        new_id: int, reads: PlannedReads, db: DbSession, t: Any = TenantScope()
    ) -> Any:
        db.add(BenchTenantItem(id=new_id, name=f"n{new_id}", value=7))
        return True

    return r


@pytest.fixture
def mx(s5: S5, sandloom_app: Any) -> S5:
    sandloom_app.include_router(_router())  # removed again by the s5 fixture
    COMMITTED.clear()
    LEAKED.clear()
    return s5


async def _post(s: S5, path: str, tenant: uuid.UUID = A) -> Any:
    resp = await s.http.post(path, headers=headers(tenant))
    await drain_post_commit()
    return resp


async def _row(s: S5, item_id: int) -> Any:
    return await s.admin.fetchrow(
        "SELECT id, name, value FROM bench_tenant_items WHERE id = $1", item_id
    )


# --------------------------------------------------------------------------- #
# Read-your-own-writes, one transaction, one connection
# --------------------------------------------------------------------------- #
@BOTH
async def test_read_your_writes_insert_and_update(mx: S5) -> None:
    r = await _post(mx, "/_m/mixed/insert_read/900001")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": 900001, "name": "n900001", "value": 7}
    assert await _row(mx, 900001) is not None  # committed with the read
    r = await _post(mx, "/_m/mixed/update_read/0")
    assert r.json()["value"] == 42  # the unflushed change is visible
    assert (await _row(mx, A_ID))["value"] == 42


@BOTH
async def test_one_connection_one_transaction_one_guc(mx: S5) -> None:
    mx.proxy.reset()
    r = await _post(mx, "/_m/mixed/insert_read/900002")
    assert r.status_code == 200, r.text
    trace = mx.proxy.reset()
    assert len({rt.conn for rt in trace}) == 1, "a second connection was used"
    sql = [" ".join(rt.sql).upper() for rt in trace]
    begins = [i for i, q in enumerate(sql) if "BEGIN" in q]
    commits = [i for i, q in enumerate(sql) if q.strip().startswith("COMMIT")]
    # the TENANT GUC only: asyncpg's one-off type introspection on a new
    # connection sets ``jit`` with set_config too, and the mark-delivered
    # UPDATE its own synchronous_commit — neither is a tenant binding
    guc = [q for q in sql if "SET_CONFIG('SANDLOOM.TENANT_ID'" in q]
    # the request transaction: ONE begin, ONE commit, ONE tenant GUC (the
    # post-commit outbox mark is a separate statement AFTER the commit)
    assert len(begins) >= 1 and len(commits) >= 1
    assert begins[0] < commits[0]
    in_tx = sql[begins[0] : commits[0] + 1]
    assert sum(q in guc for q in in_tx) == 1, in_tx
    assert not any("READ ONLY" in q for q in sql), "a read-only executor ran"
    assert len(guc) >= 1


@BOTH
async def test_rollback_undoes_the_write_and_everything_with_it(mx: S5) -> None:
    before = await _write_state(mx.admin)
    r = await _post(mx, "/_m/mixed/fail/900003")
    assert r.status_code == 409
    assert await _row(mx, 900003) is None
    assert await _write_state(mx.admin) == before  # no row, outbox, audit
    assert COMMITTED == []  # on_commit never ran
    assert mx.engine.pool.checkedout() == 0
    assert await mx.idle_in_tx() == 0


@BOTH
async def test_tenant_rules_inside_the_unit(mx: S5) -> None:
    # own (uncommitted) row: yes; under B: B's row yes, A's row no, A's
    # uncommitted row no (predicate + RLS re-synced); back under A: yes
    r = await _post(mx, "/_m/mixed/switch/900004")
    assert r.json() == [True, True, False, False, True]
    assert (await _post(mx, "/_m/mixed/cross/0")).json() is None


@BOTH
async def test_session_reads_runs_on_the_given_unit(mx: S5) -> None:
    r = await _post(mx, "/_m/session_reads/900005")
    assert r.status_code == 200 and r.json() is True


@BOTH
async def test_worker_unit_of_work(mx: S5, sandloom_core: Any) -> None:
    """db_session() + session_reads: the same mixed semantics outside HTTP."""
    with bind_tenant(A):
        async with db_session() as db:
            db.add(BenchTenantItem(id=900006, name="w", value=1))
            got = await session_reads(db).one(get_tenant_item, id=900006)
            assert got == ItemOut(id=900006, name="w", value=1)
    assert await _row(mx, 900006) is not None


# --------------------------------------------------------------------------- #
# Mixed == the same route written with plain ORM reads (side effects)
# --------------------------------------------------------------------------- #
def _shape(s: S5) -> list[str]:
    """The SQL trace with literal ids / uuids / json payloads normalised."""
    import re

    out = []
    for rt in s.proxy.reset():
        q = " ".join(rt.sql)
        q = re.sub(r"\b9000\d\d\b", "<id>", q)
        out.append(q)
    return out


@BOTH
async def test_mixed_route_side_effects_equal_the_plain_orm_route(mx: S5) -> None:
    results = {}
    # warm: first-connect dialect queries and statement caches out of the trace
    await _post(mx, "/_m/plain/insert_read/900009")
    await _post(mx, "/_m/mixed/insert_read/900008")
    for mode, new_id in (("plain", 900010), ("mixed", 900011)):
        before = await _write_state(mx.admin)
        n_outbox = await mx.admin.fetchval(
            "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NOT NULL"
        )
        COMMITTED.clear()
        mx.proxy.reset()
        r = await _post(mx, f"/_m/{mode}/insert_read/{new_id}")
        trace = _shape(mx)
        after = await _write_state(mx.admin)
        delivered = await mx.admin.fetchval(
            "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NOT NULL"
        )
        audit = await mx.admin.fetchrow(
            "SELECT action, resource_type, resource_id FROM audit_log "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
        body = r.json()
        body["id"] = body["name"] = None
        results[mode] = {
            "status": r.status_code,
            "body": body,
            "trace": trace,
            "delta": tuple(a - b for a, b in zip(after, before, strict=True)),
            "delivered": delivered - n_outbox,
            "audit": (audit["action"], audit["resource_type"]),
            "committed": [c.split(":")[0] for c in COMMITTED],
        }
    plain, mixed = results["plain"], results["mixed"]
    assert plain["status"] == mixed["status"] == 200
    assert mixed["delta"] == plain["delta"] == (1, 1, 1)  # row, outbox, audit
    assert mixed["delivered"] == plain["delivered"]
    assert mixed["committed"] == plain["committed"] == ["commit"]
    assert mixed["audit"] == plain["audit"]
    assert mixed["body"] == plain["body"]
    assert mixed["trace"] == plain["trace"], (plain["trace"], mixed["trace"])


@BOTH
async def test_failed_mixed_route_equals_failed_plain_route(mx: S5) -> None:
    for mode, new_id in (("plain", 900012), ("mixed", 900013)):
        before = await _write_state(mx.admin)
        r = await _post(mx, f"/_m/{mode}/fail/{new_id}")
        assert r.status_code == 409
        assert await _write_state(mx.admin) == before
    assert COMMITTED == []


# --------------------------------------------------------------------------- #
# Read-only routes keep their contract; both declarations are refused
# --------------------------------------------------------------------------- #
@BOTH
@pytest.mark.parametrize("action", ["event", "audit", "on_commit"])
async def test_read_only_route_still_refuses_write_primitives(
    mx: S5, action: str
) -> None:
    before = await _write_state(mx.admin)
    r = await mx.get(f"/_m/ro/{action}", headers=headers(A))
    assert r.status_code == 500
    assert await _write_state(mx.admin) == before
    assert COMMITTED == []


@BOTH
async def test_read_only_route_reads_in_a_read_only_transaction(mx: S5) -> None:
    mx.proxy.reset()
    assert (await mx.get("/_m/ro/none", headers=headers(A))).status_code == 200
    sql = " ".join(" ".join(rt.sql) for rt in mx.proxy.reset()).upper()
    if mx.variant == "planopt":
        assert "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY" in sql
    else:  # asyncpg's readonly transaction
        assert "READ ONLY" in sql or "BEGIN" in sql


@BOTH
async def test_planned_reads_plus_dbsession_is_refused(mx: S5) -> None:
    before = await _write_state(mx.admin)
    r = await _post(mx, "/_m/conflict/900014")
    assert r.status_code == 500
    assert await _write_state(mx.admin) == before
    assert await _row(mx, 900014) is None


# --------------------------------------------------------------------------- #
# Pool of ONE, cancellation, no handle sharing across requests
# --------------------------------------------------------------------------- #
@BOTH
async def test_pool_of_one_serves_mixed_routes(mx: S5) -> None:
    assert mx.engine.pool.size() == 1
    rs = await asyncio.wait_for(
        asyncio.gather(*(_post(mx, f"/_m/mixed/insert_read/{900020 + i}") for i in range(4))),
        20,
    )
    assert [r.status_code for r in rs] == [200] * 4
    assert mx.engine.pool.checkedout() == 0
    assert await mx.idle_in_tx() == 0


@BOTH
async def test_cancellation_during_the_planned_read_rolls_back(mx: S5) -> None:
    lock = mx.admin.transaction()
    await lock.start()
    try:
        await mx.admin.execute("LOCK TABLE bench_tenant_items IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_post(mx, "/_m/slow/900030"), 0.5)
    finally:
        await lock.rollback()
    for _ in range(50):  # the abandoned backend is terminated / rolled back
        if await mx.idle_in_tx() == 0 and mx.engine.pool.checkedout() == 0:
            break
        await asyncio.sleep(0.1)
    assert await mx.idle_in_tx() == 0
    assert mx.engine.pool.checkedout() == 0
    assert await _row(mx, 900030) is None
    r = await _post(mx, "/_m/mixed/insert_read/900031")  # the pool still works
    assert r.status_code == 200, r.text


@BOTH
async def test_a_reads_handle_never_outlives_its_request(mx: S5) -> None:
    assert (await _post(mx, "/_m/leak")).json() is True
    r = await _post(mx, "/_m/use_leaked")
    assert r.status_code == 500  # PlanningError: used outside its unit
    with pytest.raises(PlanningError, match="outside its Unit of Work"):
        with bind_tenant(A):
            await LEAKED[-1].one(get_tenant_item, id=A_ID)
    assert mx.engine.pool.checkedout() == 0
