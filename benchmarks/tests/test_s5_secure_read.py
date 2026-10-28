"""S5 — the secure SaaS read — correctness and security of EVERY variant.

Real PostgreSQL (``deploy/compose/test.yml``), the real request UoW,
the framework ``tenant_rls`` engine hook, FORCEd RLS on ``bench_tenant_items``
and the app connected as a NON-superuser, NOBYPASSRLS role through a
wire-protocol counting proxy, with a pool of ONE connection (so every request
reuses the previous request's connection).

Variants (``VARIANTS``):
  uow   ``/s5``     DbSession (BEGIN … COMMIT)
  ro    ``/s5ro``   ``ReadOnlyDbSession`` with ``DB_READ_ONLY_SESSIONS=true``
  fast  ``/s5fast`` raw asyncpg: ``BEGIN READ ONLY; set_config`` | SELECT | COMMIT
  fn    ``/s5fn``   raw asyncpg, autocommit, ONE statement: ``bench_s5_get()``
  plan     ``/s5plan`` declared ``ReadOperation`` via the execution planner,
                       GENERAL executor (read-only ORM session)
  planopt  ``/s5plan`` the same route, OPTIMIZED executor
                       (``PLANNER_OPTIMIZED_EXECUTOR=true``)

Known holes are pinned as ``xfail(strict=True)`` with ``HOLE:`` in the reason:
the day one is fixed the suite turns red and the marker must go.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import APIRouter, HTTPException
from sqlalchemy import column, select, table, text
from sqlalchemy.engine import make_url
from tests.perf_budgets.pgcount import PgCountingProxy

from sandloom.core.config.settings import settings
from sandloom.core.di import (
    DbSession,
    PlannedReads,
    ReadOnlyDbSession,
    TenantScope,
    get_core,
    publish_event,
    record_audit,
)
from sandloom.core.planner import ExecutionContext
from sandloom.core.testing import make_user_headers, register_event_schemas
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work.dispatch import drain_post_commit
from benchsuite import common
from benchsuite.bench import events as bench_events
from benchsuite.bench import fastread
from benchsuite.bench.api.s5 import s5_router
from benchsuite.bench.operations import get_tenant_item
from benchsuite.bench.tenant_models import BenchTenantItem
from benchsuite.setup_db import s5_statements

pytestmark = pytest.mark.asyncio

#: Cluster-wide role (roles are not per database): override it when several
#: suites run against one PostgreSQL server at once. Each pytest-xdist worker
#: gets its own (``<role>_gw<N>``): the suite counts the role's sessions in
#: pg_stat_activity, which another worker's requests must never show up in.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
ROLE = os.environ.get("S5_TEST_ROLE", "bench_app_test") + (
    f"_{_WORKER}" if _WORKER else ""
)
PASSWORD = "bench-app-test-password"
A, B = (uuid.UUID(t) for t in common.S5_TENANTS)
A_ID = 1  # owned by tenant A
B_ID = common.S5_ROWS_PER_TENANT + 1  # owned by tenant B
VARIANTS = ("uow", "ro", "fast", "fn", "plan", "planopt")
PLANNER = ("plan", "planopt")
PREFIX = {
    "uow": "/s5/",
    "ro": "/s5ro/",
    "fast": "/s5fast/",
    "fn": "/s5fn/",
    "plan": "/s5plan/",
    "planopt": "/s5plan/",
}
PROBE_SQL = "SELECT id FROM bench_tenant_items WHERE id = ANY($1::bigint[]) ORDER BY id"
_T = table("bench_tenant_items", column("id"))  # UNMAPPED: the ORM guard can't see it


def headers(tenant: uuid.UUID | None = A, **claims: Any) -> dict[str, str]:
    claims.setdefault("subscription_status", "active")
    _, h = make_user_headers(tenant_id=tenant, tenant=tenant is not None, **claims)
    return h


# --------------------------------------------------------------------------- #
# Probe routes: the SAME tenant/GUC mechanism as each variant, but the query has
# NO tenant predicate and bypasses the ORM guard — only RLS stands in the way.
# --------------------------------------------------------------------------- #
def _probe_router() -> APIRouter:
    r = APIRouter()

    def _ids(raw: str) -> list[int]:
        return [int(x) for x in raw.split(",")]

    @r.get("/_p/session/{ids}")
    async def p_session(ids: str, db: DbSession, tenant: Any = TenantScope()) -> Any:
        rows = await db.execute(
            text("SELECT id FROM bench_tenant_items WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": _ids(ids)},
        )
        return [x[0] for x in rows]

    @r.get("/_p/ro/{ids}")
    async def p_ro(ids: str, db: ReadOnlyDbSession, tenant: Any = TenantScope()) -> Any:
        stmt = select(_T.c.id).where(_T.c.id.in_(_ids(ids))).order_by(_T.c.id)
        return [x[0] for x in await db.execute(stmt)]

    @r.get("/_p/fast/{ids}")
    async def p_fast(ids: str, tenant: Any = TenantScope(), core: Any = _core()) -> Any:
        engine = core.database("default").engine
        async with fastread.driver_connection(engine) as raw:
            await raw.execute(fastread.begin_sql(tenant))
            rows = await raw.fetch(PROBE_SQL, _ids(ids))
            await raw.execute("COMMIT")
        return [x[0] for x in rows]

    @r.get("/_p/fn/{ids}")
    async def p_fn(ids: str, tenant: Any = TenantScope(), core: Any = _core()) -> Any:
        engine = core.database("default").engine
        async with fastread.driver_connection(engine) as raw:
            rows = await raw.fetch(
                "SELECT id FROM bench_s5_probe($1, $2::bigint[])", tenant, _ids(ids)
            )
        return [x[0] for x in rows]

    # planner: the SHARED executor primitives (general session / optimized raw
    # transaction) with a query that has NO tenant predicate: only RLS filters.
    @r.get("/_p/plan/{ids}")
    async def p_plan(ids: str, tenant: Any = TenantScope(), core: Any = _core()) -> Any:
        adapter = core.database("default").execution_adapter()
        async with adapter.session(_ctx(tenant)) as session:
            rows = await session.execute(
                text(
                    "SELECT id FROM bench_tenant_items WHERE id = ANY(:ids) ORDER BY id"
                ),
                {"ids": _ids(ids)},
            )
            return [x[0] for x in rows]

    @r.get("/_p/planopt/{ids}")
    async def p_planopt(
        ids: str, tenant: Any = TenantScope(), core: Any = _core()
    ) -> Any:
        adapter = core.database("default").execution_adapter()
        async with adapter.raw_transaction(_ctx(tenant)) as raw:
            rows = await raw.fetch(PROBE_SQL, _ids(ids))
        return [x[0] for x in rows]

    # -- no tenant bound at all (no TenantScope) -------------------------------
    @r.get("/_p/unbound/scope/{variant}/{ids}")
    async def u_scope(variant: str, ids: str, core: Any = _core()) -> Any:
        adapter = core.database("default").execution_adapter()
        if variant == "plan":
            async with adapter.session(_ctx(None)) as session:
                rows = await session.execute(
                    text("SELECT id FROM bench_tenant_items WHERE id = ANY(:ids)"),
                    {"ids": _ids(ids)},
                )
                return [x[0] for x in rows]
        async with adapter.raw_transaction(_ctx(None)) as raw:
            return [x[0] for x in await raw.fetch(PROBE_SQL, _ids(ids))]

    @r.get("/_p/unbound/plan/{item_id}")
    @r.get("/_p/unbound/planopt/{item_id}")
    async def u_plan(item_id: int, reads: PlannedReads) -> Any:
        item = await reads.one(get_tenant_item, id=item_id)
        return None if item is None else item.id

    @r.get("/_p/unbound/session/{ids}")
    async def u_session(ids: str, db: DbSession) -> Any:
        rows = await db.execute(
            text("SELECT id FROM bench_tenant_items WHERE id = ANY(:ids)"),
            {"ids": _ids(ids)},
        )
        return [x[0] for x in rows]

    @r.get("/_p/unbound/orm/{item_id}")
    async def u_orm(item_id: int, db: DbSession) -> Any:
        item = await db.get(BenchTenantItem, item_id)
        return None if item is None else item.id

    @r.get("/_p/unbound/ro/{ids}")
    async def u_ro(ids: str, db: ReadOnlyDbSession) -> Any:
        stmt = select(_T.c.id).where(_T.c.id.in_(_ids(ids)))
        return [x[0] for x in await db.execute(stmt)]

    @r.get("/_p/unbound/fast/{item_id}")
    async def u_fast(item_id: int, core: Any = _core()) -> Any:
        from sandloom.core.di import current_tenant_id_optional

        engine = core.database("default").engine
        row = await fastread.fetch_tx(
            engine, current_tenant_id_optional(), fastread.SQL_BY_ID, item_id, None
        )
        return None if row is None else row[0]

    @r.get("/_p/unbound/fn/{item_id}")
    async def u_fn(item_id: int, core: Any = _core()) -> Any:
        from sandloom.core.di import current_tenant_id_optional

        engine = core.database("default").engine
        row = await fastread.fetch_fn(engine, current_tenant_id_optional(), item_id)
        return None if row is None else row[0]

    # -- a request that FAILS after its tenant read (500) ----------------------
    @r.get("/_p/fail/{variant}")
    async def fail(
        variant: str, tenant: Any = TenantScope(), core: Any = _core()
    ) -> Any:
        engine = core.database("default").engine
        if variant in PLANNER:  # the failure happens INSIDE the executor scope
            adapter = core.database("default").execution_adapter()
            if variant == "plan":
                async with adapter.session(_ctx(tenant)) as session:
                    await session.execute(select(BenchTenantItem.id).limit(1))
                    raise RuntimeError("boom after the tenant read")
            async with adapter.raw_transaction(_ctx(tenant)) as raw:
                await raw.fetch(PROBE_SQL, [A_ID])
                raise RuntimeError("boom after the tenant read")
        if variant in ("fast", "fn"):
            async with fastread.driver_connection(engine) as raw:
                if variant == "fast":
                    await raw.execute(fastread.begin_sql(tenant))
                    await raw.fetch(PROBE_SQL, [A_ID])
                else:
                    await raw.fetch("SELECT * FROM bench_s5_get($1, $2)", tenant, A_ID)
                raise RuntimeError("boom after the tenant read")
        raise HTTPException(400, "use /_p/fail_session")

    @r.get("/_p/fail_session/{kind}")
    async def fail_session(
        kind: str, db: DbSession, tenant: Any = TenantScope()
    ) -> Any:
        await db.execute(select(BenchTenantItem.id).limit(1))
        raise RuntimeError("boom after the tenant read")

    @r.get("/_p/fail_ro")
    async def fail_ro(db: ReadOnlyDbSession, tenant: Any = TenantScope()) -> Any:
        await db.execute(select(BenchTenantItem.id).limit(1))
        raise RuntimeError("boom after the tenant read")

    # -- write attempts on the read-only variants -------------------------------
    @r.get("/_w/ro/{action}")
    async def w_ro(
        action: str, db: ReadOnlyDbSession, tenant: Any = TenantScope()
    ) -> Any:
        await db.get(BenchTenantItem, A_ID)
        if action == "orm":
            db.add(BenchTenantItem(id=999_001, name="x", value=1))
            await db.flush()
        elif action == "event":
            await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
        elif action == "audit":
            record_audit(
                "bench.s5.probe", actor_id=None, resource_type="x", resource_id="1"
            )
        elif action == "audit_required":
            record_audit(
                "bench.s5.probe",
                actor_id=None,
                resource_type="x",
                resource_id="1",
                required=True,
            )
        return {"ok": True}

    @r.get("/_wp/{action}")
    async def w_plan(
        action: str,
        reads: PlannedReads,
        tenant: Any = TenantScope(),
        core: Any = _core(),
    ) -> Any:
        """A route DECLARED read-only (PlannedReads) that then tries to write."""
        assert (await reads.one(get_tenant_item, id=A_ID)) is not None
        adapter = core.database("default").execution_adapter()
        if action == "orm":  # through the general executor's session
            async with adapter.session(_ctx(tenant)) as session:
                session.add(BenchTenantItem(id=999_003, name="x", value=1))
                await session.flush()
        elif action == "sql":  # through the optimized executor's transaction
            async with adapter.raw_transaction(_ctx(tenant)) as raw:
                await raw.fetchrow(
                    "INSERT INTO bench_tenant_items (id, tenant_id, name, value) "
                    "VALUES (999004, $1, 'x', 1) RETURNING id",
                    tenant,
                )
        elif action == "event":
            await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
        elif action == "audit":
            record_audit(
                "bench.s5.probe", actor_id=None, resource_type="x", resource_id="1"
            )
        elif action == "audit_required":
            record_audit(
                "bench.s5.probe",
                actor_id=None,
                resource_type="x",
                resource_id="1",
                required=True,
            )
        return {"ok": True}

    @r.get("/_w/{variant}/{action}")
    async def w_fast(
        variant: str, action: str, tenant: Any = TenantScope(), core: Any = _core()
    ) -> Any:
        engine = core.database("default").engine
        if variant == "fast":
            await fastread.fetch_tx(engine, tenant, fastread.SQL_BY_ID, A_ID, tenant)
        else:
            await fastread.fetch_fn(engine, tenant, A_ID)
        if action == "sql":
            sql = (
                "INSERT INTO bench_tenant_items (id, tenant_id, name, value) "
                "VALUES (999002, $1, 'x', 1) RETURNING id"
            )
            if variant == "fast":
                await fastread.fetch_tx(engine, tenant, sql, tenant)
            else:  # the fn variant only ever runs bench_s5_get: no SQL entry point
                raise HTTPException(400, "fn exposes no SQL entry point")
        elif action == "event":
            await publish_event(common.EVENT_CHANNEL, {"id": 1, "name": "x"})
        elif action == "audit":
            record_audit(
                "bench.s5.probe", actor_id=None, resource_type="x", resource_id="1"
            )
        elif action == "audit_required":
            record_audit(
                "bench.s5.probe",
                actor_id=None,
                resource_type="x",
                resource_id="1",
                required=True,
            )
        return {"ok": True}

    return r


def _ctx(tenant: Any) -> ExecutionContext:
    return ExecutionContext(database="default", tenant=tenant, tenant_scoped=True)


def _core() -> Any:
    from fastapi import Depends

    return Depends(get_core)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg"):
        # CI sets SANDLOOM_E2E_REQUIRED=1: a missing database must FAIL the job,
        # never silently skip the security suite.
        if os.environ.get("SANDLOOM_E2E_REQUIRED") == "1":
            pytest.fail("SANDLOOM_E2E_REQUIRED=1 but no PostgreSQL DATABASE_URL")
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


def _dsn(url: Any) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def admin(db_engine: Any) -> AsyncIterator[asyncpg.Connection]:
    """Superuser connection; (re)builds the S5 schema with RLS + the app role."""
    conn = await asyncpg.connect(_dsn(make_url(_url())))
    # The harness created bench_tenant_items from the ORM metadata WITHOUT RLS:
    # rebuild it exactly as benchsuite.setup_db does.
    await conn.execute("DROP TABLE IF EXISTS bench_tenant_items CASCADE")
    for sql in s5_statements(ROLE, PASSWORD):
        await conn.execute(sql)
    # Test-only twin of bench_s5_get WITHOUT the explicit tenant predicate:
    # proves the GUC the function sets is what RLS enforces.
    await conn.execute(
        """CREATE OR REPLACE FUNCTION bench_s5_probe(p_tenant uuid, p_ids bigint[])
        RETURNS TABLE (id bigint) LANGUAGE plpgsql SECURITY INVOKER AS $fn$
        BEGIN
          PERFORM set_config('sandloom.tenant_id', coalesce(p_tenant::text, ''), true),
                  set_config('sandloom.tenant_bypass', '', true);
          RETURN QUERY SELECT i.id FROM bench_tenant_items i
                       WHERE i.id = ANY(p_ids) ORDER BY i.id;
        END $fn$"""
    )
    await conn.execute(
        f"GRANT EXECUTE ON FUNCTION bench_s5_probe(uuid, bigint[]) TO {ROLE}"
    )
    flags = await conn.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = $1", ROLE
    )
    assert flags["rolsuper"] is False and flags["rolbypassrls"] is False
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pg_proxy() -> AsyncIterator[PgCountingProxy]:
    url = make_url(_url())
    proxy = PgCountingProxy(url.host or "127.0.0.1", int(url.port or 5432))
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.stop()


@dataclass
class S5:
    variant: str
    http: httpx.AsyncClient
    proxy: PgCountingProxy
    admin: asyncpg.Connection
    engine: Any

    @property
    def prefix(self) -> str:
        return PREFIX[self.variant]

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        resp = await self.http.get(path, **kw)
        await drain_post_commit()
        return resp

    def kinds(self) -> list[str]:
        out = []
        for rt in self.proxy.reset():
            sql = " ".join(rt.sql)
            up = sql.upper()
            if "BEGIN" in up and "SET_CONFIG" in up:
                out.append("BEGIN+SET_CONFIG")
            elif "BENCH_S5_GET" in up:
                out.append("FN")
            elif "SET_CONFIG" in up:
                out.append("SET_CONFIG")
            else:
                out.append(
                    next(
                        (
                            k
                            for k in ("BEGIN", "COMMIT", "ROLLBACK", "INSERT", "SELECT")
                            if k in up
                        ),
                        up[:40],
                    )
                )
        return out

    async def idle_in_tx(self) -> int:
        return int(
            await self.admin.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = $1 "
                "AND state LIKE 'idle in transaction%'",
                ROLE,
            )
        )

    async def pooled_state(self) -> tuple[Any, ...]:
        """(pid, in_tx, sandloom.tenant_id, sandloom.tenant_bypass) of the pooled connection."""
        async with self.engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            row = await raw.fetchrow(
                "SELECT pg_backend_pid(), "
                "coalesce(current_setting('sandloom.tenant_id', true), ''), "
                "coalesce(current_setting('sandloom.tenant_bypass', true), '')"
            )
            return row[0], raw.is_in_transaction(), row[1], row[2]


@pytest_asyncio.fixture(params=VARIANTS)
async def s5(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    sandloom_app: Any,
    sandloom_core: Any,
    admin: asyncpg.Connection,
    pg_proxy: PgCountingProxy,
) -> AsyncIterator[S5]:
    variant = request.param
    monkeypatch.setattr(settings, "db_read_only_sessions", variant == "ro")
    monkeypatch.setattr(settings, "planner_optimized_executor", variant == "planopt")
    monkeypatch.setattr(settings, "db_pool_size", 1)
    # @pytest.mark.s5_overflow(n): n overflow connections beyond the pool of
    # one, for a test whose route legitimately needs a second connection.
    overflow = request.node.get_closest_marker("s5_overflow")
    monkeypatch.setattr(
        settings, "db_max_overflow", overflow.args[0] if overflow else 0
    )
    before = list(sandloom_app.router.routes)
    # The stand mounts the bench module's routes on the platform app without
    # loading the module: register its event schemas the way the module's
    # events.EVENT_SCHEMAS would (every versioned channel needs one, SANDLOOM-R003).
    register_event_schemas(bench_events.EVENT_SCHEMAS, target=sandloom_core)
    sandloom_app.include_router(s5_router)
    sandloom_app.include_router(_probe_router())
    app_url = make_url(_url()).set(
        username=ROLE, password=PASSWORD, host="127.0.0.1", port=pg_proxy.port
    )
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                override_instance(
                    sandloom_core,
                    "database",
                    driver="sqlalchemy",
                    url=app_url.render_as_string(hide_password=False),
                    tenant_rls=True,
                )
            )
            await stack.enter_async_context(
                override_instance(
                    sandloom_core, "cache", driver="redis", url=os.environ["CACHE_URL"]
                )
            )
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=sandloom_app, raise_app_exceptions=False
                    ),
                    base_url="http://test",
                )
            )
            engine = sandloom_core.database("default").engine
            assert engine.pool.size() == 1
            sync = engine.sync_engine
            assert getattr(sync, "_sandloom_tenant_rls_installed", False)
            # the RLS hook installs no ConnectionEvents (fast execution path)
            assert sync._has_events is False
            # the read-only policy executor really is installed for ro
            assert bool(getattr(sync, "_sandloom_read_executor_installed", False)) == (
                variant == "ro"
            )
            yield S5(variant, http, pg_proxy, admin, engine)
    finally:
        sandloom_app.router.routes[:] = before


def _probe(variant: str) -> str:
    return {"uow": "session"}.get(variant, variant)


# --------------------------------------------------------------------------- #
# 1. Functional: the right row for the right tenant
# --------------------------------------------------------------------------- #
async def test_own_rows_are_served(s5: S5) -> None:
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=headers(A))
    assert r.status_code == 200, r.text
    assert r.json() == {"id": A_ID, "name": f"titem-{A_ID}", "value": 1}
    r = await s5.get(f"{s5.prefix}{B_ID}", headers=headers(B))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == B_ID
    assert (await s5.get(f"{s5.prefix}999999", headers=headers(A))).status_code == 404


async def test_cross_tenant_read_is_404(s5: S5) -> None:
    r = await s5.get(f"{s5.prefix}{B_ID}", headers=headers(A))
    assert r.status_code == 404 and "titem" not in r.text
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=headers(B))
    assert r.status_code == 404 and "titem" not in r.text


# --------------------------------------------------------------------------- #
# 2. RLS ALONE blocks a cross-tenant read (ORM guard / tenant predicate bypassed)
# --------------------------------------------------------------------------- #
async def test_rls_alone_blocks_cross_tenant_read(s5: S5) -> None:
    probe = f"/_p/{_probe(s5.variant)}/{A_ID},{B_ID}"
    r = await s5.get(probe, headers=headers(A))
    assert r.status_code == 200, r.text
    # own row visible => the GUC really reached the SELECT; other row invisible
    assert r.json() == [A_ID]
    r = await s5.get(probe, headers=headers(B))
    assert r.json() == [B_ID]


# --------------------------------------------------------------------------- #
# 3. No bound tenant: fail closed, never data
# --------------------------------------------------------------------------- #
async def test_no_tenant_fails_closed(s5: S5) -> None:
    # HTTP: a tenant-less (but valid, subscribed) token cannot select a tenant
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=headers(None))
    assert r.status_code == 400 and "titem" not in r.text
    h = headers(None)
    if s5.variant == "uow":
        # ORM path: the tenant guard raises (MissingTenantError) -> no data
        r = await s5.get(f"/_p/unbound/orm/{A_ID}", headers=h)
        assert r.status_code >= 400 and "titem" not in r.text
        # guard bypassed (raw text): RLS sees an empty GUC -> zero rows
        r = await s5.get(f"/_p/unbound/session/{A_ID},{B_ID}", headers=h)
        assert r.status_code == 200 and r.json() == []
    elif s5.variant == "ro":
        r = await s5.get(f"/_p/unbound/ro/{A_ID},{B_ID}", headers=h)
        assert r.status_code == 200 and r.json() == []
    elif s5.variant in PLANNER:
        # a tenant-scoped declared operation refuses BEFORE any database I/O
        s5.proxy.reset()
        r = await s5.get(f"/_p/unbound/{s5.variant}/{A_ID}", headers=h)
        # MissingTenantError at the request boundary: the tenant-scope 400
        # (G-TENANT-12), never data
        assert r.status_code == 400 and "titem" not in r.text
        assert s5.kinds() == [], "no tenant must mean no query at all"
        # the executor scope itself with NO tenant and a predicate-free query:
        # the GUC is '' -> RLS returns nothing (fail closed, not open)
        r = await s5.get(f"/_p/unbound/scope/{s5.variant}/{A_ID},{B_ID}", headers=h)
        assert r.status_code == 200 and r.json() == []
    else:
        # the fast paths refuse to run without a tenant (FastPathError -> 500)
        r = await s5.get(f"/_p/unbound/{s5.variant}/{A_ID}", headers=h)
        assert r.status_code == 500 and "titem" not in r.text
        # and at the SQL level NULL tenant -> zero rows
        assert await s5.admin.fetch("SET ROLE " + ROLE) == []
        try:
            assert (
                await s5.admin.fetch("SELECT * FROM bench_s5_get(NULL, $1)", A_ID) == []
            )
            assert (
                await s5.admin.fetch(
                    "SELECT * FROM bench_s5_probe(NULL, $1)", [A_ID, B_ID]
                )
                == []
            )
        finally:
            await s5.admin.execute("RESET ROLE")


# --------------------------------------------------------------------------- #
# 4. The tenant GUC is transaction-local: no leak on the reused (pool=1) connection
# --------------------------------------------------------------------------- #
async def test_tenant_guc_never_leaks_to_the_next_request(s5: S5) -> None:
    assert (await s5.get(f"{s5.prefix}{A_ID}", headers=headers(A))).status_code == 200
    pid, in_tx, tenant, bypass = await s5.pooled_state()
    assert (in_tx, tenant, bypass) == (False, "", ""), "GUC/transaction leaked"
    # next request, other tenant, SAME connection (pool of one)
    r = await s5.get(f"/_p/{_probe(s5.variant)}/{A_ID},{B_ID}", headers=headers(B))
    assert r.json() == [B_ID]
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=headers(B))
    assert r.status_code == 404
    assert (await s5.pooled_state())[0] == pid  # really the same backend
    # a FAILING request after its tenant read must not leak either
    if s5.variant in ("fast", "fn", *PLANNER):
        fail = f"/_p/fail/{s5.variant}"
    elif s5.variant == "ro":
        fail = "/_p/fail_ro"
    else:
        fail = "/_p/fail_session/x"
    assert (await s5.get(fail, headers=headers(A))).status_code == 500
    _, in_tx, tenant, bypass = await s5.pooled_state()
    assert (in_tx, tenant, bypass) == (False, "", "")
    r = await s5.get(f"/_p/{_probe(s5.variant)}/{A_ID},{B_ID}", headers=headers(B))
    assert r.json() == [B_ID]
    assert await s5.idle_in_tx() == 0


# --------------------------------------------------------------------------- #
# 5. Round trips WITH RLS, and the order proves the GUC precedes the SELECT
# --------------------------------------------------------------------------- #
EXPECTED_ROUND_TRIPS = {
    # framework tenant_rls hook: BEGIN (adapter) | set_config | SELECT | COMMIT
    "uow": ["BEGIN", "SET_CONFIG", "SELECT", "COMMIT"],
    # read-only sessions keep the transaction under tenant_rls: same as uow
    "ro": ["BEGIN", "SET_CONFIG", "SELECT", "COMMIT"],
    # BEGIN READ ONLY + set_config in one simple-protocol message
    "fast": ["BEGIN+SET_CONFIG", "SELECT", "COMMIT"],
    # one statement in autocommit
    "fn": ["FN"],
    # planner, general executor: read-only ORM session (BEGIN READ ONLY by the
    # adapter), the tenant_rls hook's set_config, the read, ROLLBACK
    "plan": ["BEGIN", "SET_CONFIG", "SELECT", "ROLLBACK"],
    # planner, optimized executor: the fast path's 3 round trips
    "planopt": ["BEGIN+SET_CONFIG", "SELECT", "COMMIT"],
}


async def test_round_trips_with_rls(s5: S5) -> None:
    url = f"{s5.prefix}{A_ID}"
    assert (await s5.get(url, headers=headers(A))).status_code == 200  # warm caches
    s5.proxy.reset()
    assert (await s5.get(url, headers=headers(A))).status_code == 200
    kinds = s5.kinds()
    assert kinds == merged_tx(EXPECTED_ROUND_TRIPS[s5.variant]), kinds


def merged_tx(kinds: list[str]) -> list[str]:
    """The pinned trips under ``DB_RLS_MERGED_BEGIN`` (default on): the
    hook's BEGIN + set_config travel as ONE message."""
    if settings.db_rls_merged_begin and kinds[:2] == ["BEGIN", "SET_CONFIG"]:
        return ["BEGIN+SET_CONFIG", *kinds[2:]]
    return kinds


# --------------------------------------------------------------------------- #
# 6. Auth / gates: 401 / 402 / 403 — and the database is never touched
# --------------------------------------------------------------------------- #
def _token(**over: Any) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "tenant_id": str(A),
        "tenant_ids": [str(A)],
        "subscription_status": "active",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("missing", 401),
        ("garbage", 401),
        ("bad_signature", 401),
        ("expired", 401),
        ("refresh_token", 401),
        ("no_iat", 401),
        ("no_subscription", 402),
        ("canceled_subscription", 402),
        ("foreign_tenant_header", 403),
        ("good", 200),
    ],
)
async def test_auth_and_gates(s5: S5, case: str, status: int) -> None:
    now = datetime.now(UTC)
    h: dict[str, str] = {}
    if case == "garbage":
        h["Authorization"] = "Bearer not-a-jwt"
    elif case == "bad_signature":
        h["Authorization"] = "Bearer " + jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "token_type": "access",
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "tenant_id": str(A),
                "tenant_ids": [str(A)],
                "subscription_status": "active",
            },
            "x" * 48,
            algorithm="HS256",
        )
    elif case == "expired":
        h["Authorization"] = "Bearer " + _token(
            iat=now - timedelta(hours=2),
            nbf=now - timedelta(hours=2),
            exp=now - timedelta(hours=1),
        )
    elif case == "refresh_token":
        h["Authorization"] = "Bearer " + _token(token_type="refresh")
    elif case == "no_iat":
        h["Authorization"] = "Bearer " + _token(iat=None)
    elif case == "no_subscription":
        h["Authorization"] = "Bearer " + _token(subscription_status=None)
    elif case == "canceled_subscription":
        h["Authorization"] = "Bearer " + _token(subscription_status="canceled")
    elif case == "foreign_tenant_header":
        h["Authorization"] = "Bearer " + _token()
        h["X-Tenant-Id"] = str(B)  # not in the signed tenant_ids allowlist
    elif case == "good":
        h["Authorization"] = "Bearer " + _token()
    # warm the pool/connection so a rejected request would show any DB I/O
    await s5.get(f"{s5.prefix}{A_ID}", headers=headers(A))
    s5.proxy.reset()
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=h)
    assert r.status_code == status, (case, r.status_code, r.text)
    if status != 200:
        assert "titem" not in r.text
        assert s5.kinds() == [], "a rejected request must not reach the database"


# --------------------------------------------------------------------------- #
# 7. Read-only variants refuse writes (never silently dropped or committed)
# --------------------------------------------------------------------------- #
async def _write_state(admin: asyncpg.Connection) -> tuple[int, int, int]:
    return (
        int(await admin.fetchval("SELECT count(*) FROM bench_tenant_items")),
        int(await admin.fetchval("SELECT count(*) FROM outbox_messages")),
        int(await admin.fetchval("SELECT count(*) FROM audit_log")),
    )


# One overflow connection: the planner route's ``orm`` / ``sql`` write opens a
# second unit (``execution_adapter().session`` / ``raw_transaction``) while
# PlannedReads holds the pooled one. On a pool of ONE that checkout waited the
# 30 s pool timeout and the test passed on the TimeoutError's 500 instead of
# the READ ONLY refusal it pins.
@pytest.mark.s5_overflow(1)
@pytest.mark.parametrize(
    "action",
    [
        "orm",
        "sql",
        "event",
        "audit",
        "audit_required",
    ],
)
async def test_read_only_variants_refuse_writes(s5: S5, action: str) -> None:
    if s5.variant == "uow":
        pytest.skip("read-write session: writes are legitimate")
    if s5.variant == "ro" and action == "sql":
        pytest.skip("covered by 'orm' (the session refuses any non-SELECT)")
    if s5.variant in ("fast", "fn") and action == "orm":
        pytest.skip("no ORM session on the fast paths")
    if s5.variant == "fn" and action == "sql":
        pytest.skip("the fn path exposes no SQL entry point")
    # planner variants: every action is REFUSED (ReadOnlyScopeError for the
    # write-path primitives; READ ONLY transaction for SQL / ORM flush). The
    # fast paths have no UoW scope: since 0.7 publish_event / record_audit
    # RAISE there (guaranteed by default) instead of publishing past the
    # outbox / silently skipping — the former HOLE_FASTPATH_SIDE_EFFECTS xfail.
    before = await _write_state(s5.admin)
    if s5.variant == "ro":
        path = f"/_w/ro/{action}"
    elif s5.variant in PLANNER:
        path = f"/_wp/{action}"
    else:
        path = f"/_w/{s5.variant}/{action}"
    s5.proxy.reset()
    r = await s5.get(path, headers=headers(A))
    kinds = s5.kinds()
    after = await _write_state(s5.admin)
    assert after == before, "a write reached the database"
    assert r.status_code >= 500, (r.status_code, r.text)
    if s5.variant in PLANNER and action in ("orm", "sql"):
        # refused BY THE DATABASE (READ ONLY transaction), not by a pool
        # timeout or an error before the statement was sent
        assert "INSERT" in kinds, kinds


# --------------------------------------------------------------------------- #
# 8. Connection lifecycle after many requests, failures included
# --------------------------------------------------------------------------- #
async def test_no_leaked_connections_or_open_transactions(s5: S5) -> None:
    probe = _probe(s5.variant)
    fail = {
        "fast": "/_p/fail/fast",
        "fn": "/_p/fail/fn",
        "ro": "/_p/fail_ro",
        "plan": "/_p/fail/plan",
        "planopt": "/_p/fail/planopt",
    }.get(s5.variant, "/_p/fail_session/x")
    codes = []
    for i in range(6):
        tenant, own, other = (A, A_ID, B_ID) if i % 2 == 0 else (B, B_ID, A_ID)
        h = headers(tenant)
        codes += [
            (await s5.get(f"{s5.prefix}{own}", headers=h)).status_code,
            (await s5.get(f"{s5.prefix}{other}", headers=h)).status_code,
            (await s5.get(f"{s5.prefix}{own}")).status_code,
            (
                await s5.get(
                    f"{s5.prefix}{own}",
                    headers=headers(tenant, subscription_status=None),
                )
            ).status_code,
            (await s5.get(fail, headers=h)).status_code,
            (await s5.get(f"/_p/{probe}/{A_ID},{B_ID}", headers=h)).status_code,
        ]
    assert sorted(set(codes)) == [200, 401, 402, 404, 500], codes
    assert s5.engine.pool.checkedout() == 0
    assert await s5.idle_in_tx() == 0
    _, in_tx, tenant, bypass = await s5.pooled_state()
    assert (in_tx, tenant, bypass) == (False, "", "")


async def test_fast_path_cancellation_never_pools_an_open_transaction(s5: S5) -> None:
    """Client disconnect / timeout mid-query: the connection is invalidated."""
    import asyncio

    if s5.variant not in ("fast", "fn"):
        pytest.skip("raw-driver fast paths only")
    pid = (await s5.pooled_state())[0]

    async def slow() -> Any:
        if s5.variant == "fast":
            return await fastread.fetch_tx(
                s5.engine,
                A,
                "SELECT pg_sleep(2), id FROM bench_tenant_items WHERE id = $1",
                A_ID,
            )
        async with fastread.driver_connection(s5.engine) as raw:
            return await raw.fetchrow(
                "SELECT pg_sleep(2), * FROM bench_s5_get($1, $2)", A, A_ID
            )

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(slow(), 0.2)
    pid2, in_tx, tenant, bypass = await s5.pooled_state()
    assert pid2 != pid, "the cancelled connection went back to the pool"
    assert (in_tx, tenant, bypass) == (False, "", "")
    assert s5.engine.pool.checkedout() == 0
    assert await s5.idle_in_tx() == 0
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=headers(B))
    assert r.status_code == 404


async def test_raw_begin_is_invisible_to_the_pool_reset(s5: S5) -> None:
    """WHY the fast path invalidates: SQLAlchemy's reset-on-return ROLLBACK is a
    no-op for a BEGIN it did not send, so a raw transaction (with tenant A's
    GUC) would be handed to the next checkout."""
    if s5.variant != "fast":
        pytest.skip("demonstrated once")
    async with s5.engine.connect() as conn:  # NO fastread guard
        raw = (await conn.get_raw_connection()).driver_connection
        await raw.execute(fastread.begin_sql(A))
    _, in_tx, tenant, _ = await s5.pooled_state()
    assert in_tx is True and tenant == str(A), "pool reset now covers raw BEGIN?"
    # the guarded fast path refuses such a connection instead of using it
    r = await s5.get(f"{s5.prefix}{A_ID}", headers=headers(A))
    assert r.status_code == 500
    _, in_tx, tenant, _ = await s5.pooled_state()
    assert (in_tx, tenant) == (False, "")


# --------------------------------------------------------------------------- #
# 9. The autocommit trap and the CTE evaluation-order pitfall (SQL level)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    url = make_url(_url()).set(username=ROLE, password=PASSWORD)
    conn = await asyncpg.connect(_dsn(url))
    try:
        yield conn
    finally:
        await conn.close()


CTE_BY_ID = (
    "WITH cfg AS MATERIALIZED (SELECT set_config('sandloom.tenant_id', $1, true) AS t) "
    "SELECT i.id FROM cfg, bench_tenant_items i WHERE i.id = $2"
)
CTE_LIST = (
    "WITH cfg AS MATERIALIZED (SELECT set_config('sandloom.tenant_id', $1, true) AS t) "
    "SELECT i.id, i.tenant_id FROM cfg, bench_tenant_items i ORDER BY i.id LIMIT 3"
)


async def test_autocommit_set_config_is_gone_for_the_next_statement(
    app_conn: asyncpg.Connection,
) -> None:
    """THE trap: set_config(..., true) in autocommit lives for ONE statement."""
    assert not app_conn.is_in_transaction()
    assert await app_conn.fetchval(
        "SELECT set_config('sandloom.tenant_id', $1, true)", str(A)
    ) == str(A)
    assert await app_conn.fetchval("SELECT current_setting('sandloom.tenant_id', true)") in (
        "",
        None,
    )
    # ... so the next SELECT sees an empty GUC: zero rows (fails CLOSED, not open)
    assert (
        await app_conn.fetch("SELECT id FROM bench_tenant_items WHERE id = $1", A_ID)
        == []
    )
    # and it does not error-open with a bogus value either
    assert await app_conn.fetchval("SELECT count(*) FROM bench_tenant_items") == 0


async def test_cte_set_config_is_plan_dependent(app_conn: asyncpg.Connection) -> None:
    """One-statement CTE: works for the PK plan, silently empty for a scan plan."""
    # PK lookup: nested loop with the CTE outer -> GUC set before the policy runs
    assert [r["id"] for r in await app_conn.fetch(CTE_BY_ID, str(A), A_ID)] == [A_ID]
    assert await app_conn.fetch(CTE_BY_ID, str(A), B_ID) == []
    # list: the outcome depends on the PLAN. With the table scan OUTER the
    # policy reads the GUC BEFORE the CTE ran -> empty (fail closed, but WRONG:
    # data missing); the bypass-free policy (CASE ... ~ regex) currently gets the
    # CTE outer -> B's rows. Either way never another tenant's row here (the
    # GUC starts empty in autocommit); the stale-GUC test below is the fail-open.
    rows = await app_conn.fetch(CTE_LIST, str(B))
    assert {r["tenant_id"] for r in rows} <= {B}


async def test_cte_set_config_fails_open_on_a_stale_transaction_guc(
    app_conn: asyncpg.Connection,
) -> None:
    """Inside a transaction whose GUC still says tenant A, a CTE that 'switches'
    to tenant B leaks tenant A's row (policy evaluated before the CTE)."""
    async with app_conn.transaction():
        await app_conn.execute(f"SELECT set_config('sandloom.tenant_id', '{A}', true)")
        rows = await app_conn.fetch(CTE_LIST, str(B))
        tenants = {r["tenant_id"] for r in rows}
    assert A in tenants, "expected the documented fail-open (plan changed?)"


async def test_plpgsql_function_is_order_safe(app_conn: asyncpg.Connection) -> None:
    """bench_s5_get sets the GUC in its OWN statement before its query: correct
    in autocommit AND on a stale transaction GUC; the GUC dies with autocommit."""
    assert [
        r["id"]
        for r in await app_conn.fetch("SELECT * FROM bench_s5_get($1, $2)", A, A_ID)
    ] == [A_ID]
    assert await app_conn.fetch("SELECT * FROM bench_s5_get($1, $2)", A, B_ID) == []
    assert await app_conn.fetchval("SELECT current_setting('sandloom.tenant_id', true)") in (
        "",
        None,
    )
    async with app_conn.transaction():
        await app_conn.execute(f"SELECT set_config('sandloom.tenant_id', '{A}', true)")
        rows = await app_conn.fetch(
            "SELECT * FROM bench_s5_probe($1, $2)", B, [A_ID, B_ID]
        )
        assert [r["id"] for r in rows] == [B_ID]


# --------------------------------------------------------------------------- #
# 9b. Audit of the plpgsql function itself (bench_s5_get)
# --------------------------------------------------------------------------- #
async def test_function_attributes_invoker_pinned_path_no_public_execute(
    admin: asyncpg.Connection,
) -> None:
    row = await admin.fetchrow(
        "SELECT p.prosecdef, p.proconfig, p.proacl::text[] AS acl "
        "FROM pg_proc p WHERE p.oid = 'public.bench_s5_get(uuid, bigint)'::regprocedure"
    )
    assert row["prosecdef"] is False  # SECURITY INVOKER: RLS applies to the caller
    assert row["proconfig"] == ["search_path=pg_catalog, public, pg_temp"]
    # No PUBLIC grant ("=X/owner"); the app role has EXECUTE.
    assert not any(a.startswith("=") for a in row["acl"]), row["acl"]
    assert any(a.startswith(f"{ROLE}=X/") for a in row["acl"]), row["acl"]
    stranger = f"s5_stranger_{uuid.uuid4().hex[:8]}"
    await admin.execute(f"CREATE ROLE {stranger} NOLOGIN")
    try:
        assert not await admin.fetchval(
            "SELECT has_function_privilege($1, "
            "'public.bench_s5_get(uuid, bigint)', 'EXECUTE')",
            stranger,
        )
    finally:
        await admin.execute(f"DROP ROLE {stranger}")


async def test_force_rls_applies_inside_the_function(admin: asyncpg.Connection) -> None:
    """Replace the policy by USING (false) in a rolled-back transaction and call
    the function AS the app role: zero rows even for the tenant's own row —
    the policy, not only the function's own predicate, filters inside it."""
    tr = admin.transaction()
    await tr.start()
    try:
        assert [
            r["id"]
            for r in await admin.fetch("SELECT * FROM bench_s5_get($1, $2)", A, A_ID)
        ] == [A_ID]  # superuser: RLS ignored
        await admin.execute(
            "ALTER POLICY bench_tenant_items_tenant_isolation ON bench_tenant_items "
            "USING (false) WITH CHECK (false)"
        )
        await admin.execute(f"SET LOCAL ROLE {ROLE}")
        assert await admin.fetch("SELECT * FROM bench_s5_get($1, $2)", A, A_ID) == []
    finally:
        await tr.rollback()


async def test_function_cannot_be_shadowed_by_a_temp_table(
    app_conn: asyncpg.Connection,
) -> None:
    await app_conn.execute(
        "CREATE TEMP TABLE bench_tenant_items (id bigint, tenant_id uuid, "
        "name varchar, value integer)"
    )
    await app_conn.execute(
        "INSERT INTO pg_temp.bench_tenant_items VALUES ($1, $2, 'evil', -1)", A_ID, A
    )
    rows = await app_conn.fetch("SELECT * FROM bench_s5_get($1, $2)", A, A_ID)
    assert [(r["id"], r["name"]) for r in rows] == [(A_ID, f"titem-{A_ID}")]


async def test_function_does_not_change_an_open_transactions_tenant(
    app_conn: asyncpg.Connection,
) -> None:
    """Called inside a transaction bound to tenant B, it reads A's row for the
    caller-supplied A and RESTORES the transaction's GUC to B afterwards."""
    async with app_conn.transaction():
        await app_conn.execute(f"SELECT set_config('sandloom.tenant_id', '{B}', true)")
        rows = await app_conn.fetch("SELECT * FROM bench_s5_get($1, $2)", A, A_ID)
        assert [r["id"] for r in rows] == [A_ID]
        assert await app_conn.fetchval(
            "SELECT current_setting('sandloom.tenant_id', true)"
        ) == str(B)
        ids = await app_conn.fetch(
            "SELECT id FROM bench_tenant_items WHERE id = $1", A_ID
        )
        assert ids == []  # still tenant B: A's row invisible again
    assert await app_conn.fetch("SELECT * FROM bench_s5_get(NULL, $1)", A_ID) == []


async def test_fn_endpoint_takes_the_tenant_from_the_token_not_the_client(
    s5: S5,
) -> None:
    if s5.variant != "fn":
        pytest.skip("fn variant only")
    # X-Tenant-Id naming B with a token for A only: 403 (selector outside the
    # signed tenant_ids); B's id with A's token: 404 (the function got A).
    r = await s5.http.get(
        f"/s5fn/{A_ID}", headers={**headers(A), "X-Tenant-Id": str(B)}
    )
    assert r.status_code == 403
    r = await s5.http.get(f"/s5fn/{B_ID}", headers=headers(A))
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# 10. Redis round trips per S5 request (the auth side of the security cost)
# --------------------------------------------------------------------------- #
#: get_current_user_id, TenantScope and require() all check the subject-state
#: record (revocation and block in ONE key, ADR-0009), memoised per request:
#: ONE GET (JWT verify itself: no I/O).
EXPECTED_REDIS = ["GET"]


async def test_redis_round_trips(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await sandloom_core.cache().raw_client()
    sent: list[str] = []
    original = client.execute_command

    async def counting(*args: Any, **kwargs: Any) -> Any:
        sent.append(str(args[0]).upper())
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "execute_command", counting)
    url = f"{s5.prefix}{A_ID}"
    assert (await s5.get(url, headers=headers(A))).status_code == 200
    sent.clear()
    assert (await s5.get(url, headers=headers(A))).status_code == 200
    assert sent == EXPECTED_REDIS, sent
