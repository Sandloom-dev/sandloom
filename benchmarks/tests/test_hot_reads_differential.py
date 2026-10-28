"""Differential harness: the hot read routes, ORM/UoW path vs declared reads.

Each ported route is served TWICE per case by the SAME app: once by its
reference implementation — the ``DbSession`` handler as it was before the port
(copied verbatim from ``main`` at ``e39d66ea``, below) — and once by the real
route, which now runs declared operations through ``PlannedReads``. The swap
replaces the route's ASGI ``app`` only (path, dependencies, response model and
status code are the route's own), alternating which side goes first.

Per request it compares (``Observation``):

* status, body bytes, the FULL raw response header list in order (values of
  ``x-request-id`` / ``x-process-time`` masked, their position kept);
* the exception (if any) that escaped the ASGI app;
* the Redis command trace (revocation / block marks, link cache and counter);
* Prometheus deltas (every changed sample by name + labels; counters and
  histogram ``_count`` by value — ``_sum`` / buckets are timing);
* ``outbox_messages`` / ``audit_log`` row deltas;
* the pooled connection after the request (in a transaction? tenant GUC),
  ``idle in transaction`` sessions and kernel contextvars (nothing may leak).

The PostgreSQL protocol trace is the one thing that is SUPPOSED to change: it is
asserted, not compared — the same tables read by the same number of data
SELECTs, every transaction of the declared path ``READ ONLY`` (and READ
COMMITTED), no DML, the tenant GUC set for a tenant-scoped read, and no SQL at
all where the old path had none (401s, link cache hits).

Stand: the S5 stand of ``test_s5_secure_read`` (the app connected as a
NON-superuser NOBYPASSRLS role through the counting proxy, ONE pooled
connection), the platform modules plus the example apps' modules
(``benchsuite.mix.FULL_MODULES``) in one app, FORCEd RLS on the tenant-scoped
``notifications_inapp``. Both executors: ``planopt`` (optimized) and ``plan``
(general, the ORM in a read-only unit).

Run (own DB / Redis index)::

    PYTHONPATH=../benchmarks:src uv run pytest -c pyproject.toml \\
        ../benchmarks/tests/test_hot_reads_differential.py -q --no-cov
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Depends, Query
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_s5_secure_read import (  # noqa: F401 - fixtures
    PASSWORD,
    ROLE,
    _dsn,
    _token,
    _url,
    pg_proxy,
)
from tests.perf_budgets.pgcount import PgCountingProxy

from sandloom.core import tenant_context as _tenant_ctx
from sandloom.core.auth.revocation import revoke_user_tokens
from sandloom.core.config.settings import settings
from sandloom.core.di import DbSession, TenantScope, cache
from sandloom.core.module_api import _context as _module_api_context
from sandloom.core.observability.request_context import request_id_var
from sandloom.core.route_markers import prune_marker_dependencies
from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work import dispatch as _uow_dispatch
from sandloom.core.unit_of_work import scope as _uow_scope
from sandloom.shared.guards.auth import get_current_user_id
from benchsuite import mix
from benchsuite.setup_db import s5_statements

pytestmark = pytest.mark.asyncio

MASKED = {b"x-request-id", b"x-process-time"}
ORG = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
OTHER_ORG = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
LINK_CODE = "hotcode"
N_TICKETS = 7
N_NOTES = 6


# --------------------------------------------------------------------------- #
# Reference implementations: the handlers as they were BEFORE the port
# (main @ e39d66ea), on the request Unit of Work.
# --------------------------------------------------------------------------- #
def _reference_handlers() -> dict[tuple[str, str], Callable[..., Any]]:
    from helpdesk.modules.tickets.api.tickets import _membership_service
    from helpdesk.modules.tickets.models import Ticket
    from helpdesk.modules.tickets.schemas import TicketOut
    from helpdesk.modules.tickets.services import TicketService
    from linkly.modules.links.models import Shortlink
    from projecthub.modules.notifications.models import NotificationRecord
    from projecthub.modules.notifications.schemas import NotificationOut

    from sandloom_identity.schemas.user import UserOut
    from sandloom_identity.services.user_service import UserService

    async def get_current_user_info(
        db: DbSession,
        user_id: UUID = Depends(get_current_user_id),
    ) -> UserOut:
        user = await UserService.get_by_id(user_id, db)
        return UserOut.model_validate(user)

    async def _old_list_for_org(
        membership: Any, organization_id: UUID, user_id: UUID, db: Any, limit: int
    ) -> list[Ticket]:
        await TicketService._ensure_member(membership, organization_id, user_id)
        result = await db.execute(
            select(Ticket)
            .where(Ticket.organization_id == organization_id)
            .order_by(Ticket.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_tickets(
        organization_id: UUID,
        db: DbSession,
        user_id: UUID = Depends(get_current_user_id),
        membership: Any = Depends(_membership_service),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[TicketOut]:
        tickets = await _old_list_for_org(
            membership, organization_id, user_id, db, limit
        )
        return [TicketOut.model_validate(t) for t in tickets]

    async def export_tickets(
        organization_id: UUID,
        db: DbSession,
        user_id: UUID = Depends(get_current_user_id),
        membership: Any = Depends(_membership_service),
    ) -> dict[str, Any]:
        tickets = await _old_list_for_org(membership, organization_id, user_id, db, 100)
        return {
            "organization_id": str(organization_id),
            "count": len(tickets),
            "tickets": [
                TicketOut.model_validate(t).model_dump(mode="json") for t in tickets
            ],
        }

    async def list_notifications(
        db: DbSession,
        _tenant_id: UUID = TenantScope(),
        _user_id: UUID = Depends(get_current_user_id),
    ) -> list[NotificationOut]:
        stmt = (
            select(NotificationRecord)
            .order_by(NotificationRecord.created_at.desc())
            .limit(50)
        )
        rows = (await db.execute(stmt)).scalars().all()
        return [NotificationOut.model_validate(r) for r in rows]

    async def resolve_link(code: str, db: DbSession) -> RedirectResponse:
        from fastapi import HTTPException, status

        c = cache()
        cached = await c.get(f"links:url:{code}")
        if cached is not None:
            target = cached
        else:
            result = await db.execute(select(Shortlink).where(Shortlink.code == code))
            link = result.scalar_one_or_none()
            if link is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="short code not found"
                )
            target = link.target_url
            await c.set(f"links:url:{code}", target, ttl_seconds=3600)
        await c.incr(f"links:clicks:{code}")
        return RedirectResponse(url=target, status_code=307)

    return {
        ("GET", "/auth/me"): get_current_user_info,
        ("GET", "/api/v1/orgs/{organization_id}/tickets"): list_tickets,
        ("GET", "/api/v1/orgs/{organization_id}/tickets/export"): export_tickets,
        ("GET", "/api/v1/notifications"): list_notifications,
        ("GET", "/api/v1/links/{code}"): resolve_link,
    }


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #
_CTXVARS = {
    "tenant": _tenant_ctx._current_tenant,
    "bypass": _tenant_ctx._bypass,
    "request_id": request_id_var,
    "uow": _uow_scope._current_uow_session,
    "uow_sessions": _uow_scope._active_uow_sessions,
    "read_only_scope": _uow_scope._read_only_scope,
    "contract_session": _module_api_context._contract_session_var,
    "read_scope": _module_api_context._read_scope_var,
}


def _ctx_snapshot() -> dict[str, Any]:
    return {k: v.get(None) for k, v in _CTXVARS.items()}


def _metrics() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    out = {}
    for fam in REGISTRY.collect():
        for s in fam.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def _metric_delta(before: dict[Any, float], after: dict[Any, float]) -> list[Any]:
    delta = []
    for key, val in after.items():
        old = before.get(key, 0.0)
        if val == old:
            continue
        name = key[0]
        if name.startswith(("python_", "process_")) or name.endswith(
            ("_created", "_bucket")
        ):
            continue  # interpreter noise / first-seen timestamps / timing buckets
        delta.append((key, "changed" if name.endswith("_sum") else val - old))
    return sorted(delta, key=repr)


@dataclass
class Observation:
    status: int | None
    headers: list[tuple[bytes, bytes]]
    body: bytes
    exception: str | None
    redis: list[tuple[Any, ...]]
    metrics: list[Any]
    outbox_audit: tuple[int, int]
    pool: tuple[Any, ...]
    idle_in_tx: int
    ctx_leak: dict[str, Any]
    #: asserted, not compared (see the module doc)
    sql: list[str] = field(default_factory=list)


COMPARED = (
    "status",
    "headers",
    "body",
    "exception",
    "redis",
    "metrics",
    "outbox_audit",
    "pool",
    "idle_in_tx",
    "ctx_leak",
)


def assert_same(a: Observation, b: Observation, case: str) -> None:
    diffs = {
        k: (getattr(a, k), getattr(b, k))
        for k in COMPARED
        if getattr(a, k) != getattr(b, k)
    }
    assert not diffs, f"{case}: DIVERGENCE {diffs}"
    assert a.ctx_leak == {}, f"{case}: contextvar leaked {a.ctx_leak}"


_FROM = re.compile(r'\bFROM\s+"?([a-z_]+)"?', re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def _data_reads(sql: list[str]) -> list[str]:
    """The tables of the data SELECTs of a trace (one entry per SELECT)."""
    out = []
    for rt in sql:
        for stmt in rt.split(" | "):
            up = stmt.strip().upper()
            if not up.startswith("SELECT") or "SET_CONFIG" in up:
                continue
            if "PG_CATALOG" in up or "PG_TYPE" in up:
                continue  # driver type introspection (connection warmth)
            out.extend(sorted(set(_FROM.findall(stmt))))
    return out


def _begins(sql: list[str]) -> Counter[str]:
    return Counter(
        stmt.strip()
        for rt in sql
        for stmt in rt.split(" | ")
        if stmt.strip().upper().startswith("BEGIN")
    )


def assert_sql(
    old: list[str],
    new: list[str],
    case: str,
    *,
    tenant: Any,
    contract_units: int = 0,
) -> None:
    """The declared path reads what the UoW path read, read-only, no DML.

    * the same tables, in the same order, by the same number of data SELECTs;
    * no INSERT / UPDATE / DELETE;
    * every transaction the port ADDED is ``READ ONLY`` and none is writable
      any more (the contract reads a route makes — e.g. the tickets'
      membership check — are the same on both sides);
    * no SQL at all where the old path had none;
    * a tenant-scoped read sets the tenant GUC (and names the tenant on the
      optimized executor, where it is part of the BEGIN message).

    ``contract_units``: writable transactions of contract READS the route's
    gate makes in their own unit (a live organization gate: billing's facts),
    present identically on the old side: exactly that many, never more.
    """
    assert _data_reads(old) == _data_reads(new), (case, old, new)
    for rt in new:
        for stmt in rt.split(" | "):
            assert not _DML.match(stmt), (case, stmt)
    if not old:
        assert new == [], (case, new)
        return
    b_old, b_new = _begins(old), _begins(new)
    added = b_new - b_old
    assert all("READ ONLY" in s.upper() for s in added), (case, added)
    writable = [
        sum(n for s, n in b.items() if "READ ONLY" not in s.upper())
        for b in (b_old, b_new)
    ]
    # the old path's request UoW was the only writable transaction: gone
    assert writable[1] == contract_units, (case, b_old, b_new)
    if contract_units:
        w_old = Counter(
            {s: n for s, n in b_old.items() if "READ ONLY" not in s.upper()}
        )
        w_new = Counter(
            {s: n for s, n in b_new.items() if "READ ONLY" not in s.upper()}
        )
        assert not (w_new - w_old), (case, b_old, b_new)
    if tenant is not None:
        assert any("set_config" in rt for rt in new), (case, new)
        if any("READ ONLY; SELECT set_config" in rt for rt in new):
            assert any(str(tenant) in rt for rt in new), (case, new)


@dataclass
class Hot:
    app: Any
    core: Any
    routes: dict[tuple[str, str], tuple[APIRoute, Any, Any]]  # (route, new, old)
    proxy: PgCountingProxy
    admin: asyncpg.Connection
    redis_log: list[tuple[Any, ...]]
    executor: str
    user: uuid.UUID
    member: dict[str, str]
    warm_pid: int | None = None

    def use(self, new: bool) -> None:
        for route, new_app, old_app in self.routes.values():
            route.app = new_app if new else old_app

    async def pool_state(self) -> tuple[Any, ...]:
        engine = self.core.database("default").engine
        async with engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            row = await raw.fetchrow(
                "SELECT pg_backend_pid(), "
                "coalesce(current_setting('sandloom.tenant_id', true), '')"
            )
            return row[0], raw.is_in_transaction(), row[1]

    async def counts(self) -> tuple[int, int]:
        return (
            int(await self.admin.fetchval("SELECT count(*) FROM outbox_messages")),
            int(await self.admin.fetchval("SELECT count(*) FROM audit_log")),
        )

    async def call(self, path: str, hdrs: dict[str, str] | None) -> Observation:
        pid0, _, _ = await self.pool_state()
        counts0 = await self.counts()
        m0 = _metrics()
        ctx0 = _ctx_snapshot()
        self.proxy.reset()
        self.redis_log.clear()
        raw_path, _, qs = path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": qs.encode(),
            "root_path": "",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in (hdrs or {}).items()
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        msgs: list[dict[str, Any]] = []

        async def send(msg: dict[str, Any]) -> None:
            msgs.append(msg)

        exc_name = None
        try:
            await self.app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001
            exc_name = type(exc).__name__
        ctx1 = _ctx_snapshot()
        await _uow_dispatch.drain_post_commit()
        status, hdr_list, body = None, [], b""
        for m in msgs:
            if m["type"] == "http.response.start":
                status = m["status"]
                hdr_list = [
                    (k, b"*" if k.lower() in MASKED else v) for k, v in m["headers"]
                ]
            elif m["type"] == "http.response.body":
                body += m.get("body", b"")
        sql = [
            " | ".join(
                dict.fromkeys(q.removeprefix("P:").removeprefix("B:") for q in rt.sql)
            )
            for rt in self.proxy.reset()
        ]
        pid1, in_tx, guc = await self.pool_state()
        return Observation(
            status=status,
            headers=hdr_list,
            body=body,
            exception=exc_name,
            redis=list(self.redis_log),
            metrics=_metric_delta(m0, _metrics()),
            outbox_audit=tuple(  # type: ignore[arg-type]
                b - a for a, b in zip(counts0, await self.counts(), strict=True)
            ),
            # (pooled connection replaced?, in a transaction?, tenant GUC)
            pool=(pid1 != pid0, in_tx, guc),
            idle_in_tx=int(
                await self.admin.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE usename = $1 "
                    "AND datname = current_database() "
                    "AND state LIKE 'idle in transaction%'",
                    ROLE,
                )
            ),
            ctx_leak={k: v for k, v in ctx1.items() if v != ctx0[k]},
            sql=sql,
        )

    async def both(
        self, path: str, hdrs: dict[str, str] | None, *, first: str = "old"
    ) -> tuple[Observation, Observation]:
        order = ["old", "new"] if first == "old" else ["new", "old"]
        out: dict[str, Observation] = {}
        for side in order:
            self.use(side == "new")
            out[side] = await self.call(path, hdrs)
        self.use(True)
        return out["old"], out["new"]

    async def check(
        self,
        name: str,
        path: str,
        hdrs: dict[str, str] | None,
        *,
        first: str = "old",
        tenant: Any = None,
        contract_units: int = 0,
    ) -> Observation:
        old, new = await self.both(path, hdrs, first=first)
        assert_same(old, new, name)
        assert_sql(old.sql, new.sql, name, tenant=tenant, contract_units=contract_units)
        return old


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def _user_token(user: uuid.UUID, **over: Any) -> str:
    claims: dict[str, Any] = {
        "sub": str(user),
        "tenant_id": None,
        "tenant_ids": [str(ORG)],
    }
    claims.update(over)
    return _token(**claims)


#: ORG's billing customer: the helpdesk export gate is the ORGANIZATION's
#: live subscription (spec §7, ``Subscribed(of=Account.org(...))``)
ORG_CUSTOMER = uuid.UUID("00000000-0000-4000-8000-0000000000e1")


async def _seed_org_subscription(admin: asyncpg.Connection) -> None:
    await admin.execute(
        "INSERT INTO billing_products (id, name, type, is_active) VALUES"
        " (951, 'Hot Team', 'subscription', true) ON CONFLICT DO NOTHING"
    )
    await admin.execute(
        "INSERT INTO billing_prices (id, product_id, amount, currency,"
        " recurring_interval, stripe_price_id) VALUES"
        " (961, 951, 9900, 'USD', 'month', 'price_hot_team') ON CONFLICT DO NOTHING"
    )
    await admin.execute(
        "INSERT INTO billing_customers (id, user_id, organization_id)"
        " VALUES ($1, NULL, $2) ON CONFLICT DO NOTHING",
        ORG_CUSTOMER,
        ORG,
    )
    await admin.execute(
        "INSERT INTO billing_subscriptions (customer_id, price_id, status,"
        " current_period_end, cancel_at_period_end, origin)"
        " VALUES ($1, 961, 'active', now() + interval '20 days', false, 'intent')",
        ORG_CUSTOMER,
    )


async def _seed(admin: asyncpg.Connection, user: uuid.UUID) -> None:
    now = datetime.now(UTC)
    await admin.execute(
        """INSERT INTO users (id, email, full_name, is_active, is_superuser,
               is_verified, created_at, updated_at)
           VALUES ($1, 'Hot.Reads@Example.COM', 'Hot Reads', true, false, true,
                   $2, $2)""",
        user,
        now,
    )
    for org, owner in ((ORG, user), (OTHER_ORG, uuid.uuid4())):
        await admin.execute(
            """INSERT INTO tenancy_organizations
                   (id, name, slug, owner_id, is_active, created_at, updated_at)
               VALUES ($1, 'Hot Org', $2, $3, true, now(), now())""",
            org,
            f"hot-{org.hex[-12:]}",
            owner,
        )
        await admin.execute(
            """INSERT INTO tenancy_members (id, organization_id, user_id, role,
                   created_at) VALUES ($1, $2, $3, 'owner', now())""",
            uuid.uuid4(),
            org,
            owner,
        )
    await admin.executemany(
        "INSERT INTO tickets_tickets (id, organization_id, created_by, subject, body,"
        " status, priority, created_at, updated_at) VALUES"
        " ($1, $2, $3, $4, 'b', 'open', 'normal', $5, $5)",
        [
            (uuid.uuid4(), org, user, f"t{i}", now - timedelta(seconds=i))
            for org in (ORG, OTHER_ORG)
            for i in range(N_TICKETS)
        ],
    )
    await admin.executemany(
        "INSERT INTO notifications_inapp (id, tenant_id, recipient_id, kind, title,"
        " body, created_at) VALUES ($1, $2, $3, 'k', $4, 'b', $5)",
        [
            (uuid.uuid4(), tenant, user, f"n{i}", now - timedelta(seconds=i))
            for tenant in (ORG, OTHER_ORG)
            for i in range(N_NOTES)
        ],
    )
    await admin.execute(
        "INSERT INTO links_shortlinks (id, code, target_url, clicks, created_at)"
        " VALUES ($1, $2, 'https://example.com/hot', 0, now())",
        uuid.uuid4(),
        LINK_CODE,
    )
    await _seed_org_subscription(admin)


def _examples_on_path() -> None:
    for app in ("helpdesk", "projecthub", "linkly"):
        root = os.path.join(mix.EXAMPLES, app)
        if root not in sys.path:
            sys.path.insert(0, root)


@pytest_asyncio.fixture(params=["planopt", "plan"])
async def hot(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    sandloom_app: Any,  # the session app first: this test builds its own afterwards
    pg_proxy: PgCountingProxy,  # noqa: F811
) -> AsyncIterator[Hot]:
    import redis.asyncio.client as rclient

    from sandloom.core.testing.database import open_test_engine

    _examples_on_path()
    monkeypatch.setattr(
        settings, "planner_optimized_executor", request.param == "planopt"
    )
    monkeypatch.setattr(settings, "db_pool_size", 1)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    monkeypatch.setattr(settings, "sandloom_modules", list(mix.FULL_MODULES))
    from sandloom.main import create_app

    app = create_app()
    core = app.state.core
    redis_log: list[tuple[Any, ...]] = []
    orig = rclient.Redis.execute_command

    async def traced(self: Any, *args: Any, **options: Any) -> Any:
        redis_log.append(tuple(str(a) for a in args))
        return await orig(self, *args, **options)

    monkeypatch.setattr(rclient.Redis, "execute_command", traced)
    references = _reference_handlers()
    routes: dict[tuple[str, str], tuple[APIRoute, Any, Any]] = {}
    for r in app.router.routes:
        for method in getattr(r, "methods", None) or ():
            if (method, getattr(r, "path", None)) in references:
                old = APIRoute(
                    r.path,
                    references[(method, r.path)],
                    response_model=r.response_model,
                    status_code=r.status_code,
                    dependencies=list(r.dependencies),
                    methods=[method],
                    response_class=r.response_class,
                    name=r.name,
                )
                prune_marker_dependencies([old])
                routes[(method, r.path)] = (r, r.app, old.app)
    assert routes.keys() == references.keys(), routes.keys()
    user = uuid.uuid4()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(open_test_engine())
        admin = await asyncpg.connect(_dsn(make_url(_url())))
        stack.push_async_callback(admin.close)
        await admin.execute("DROP TABLE IF EXISTS bench_tenant_items CASCADE")
        for sql in s5_statements(ROLE, PASSWORD):
            await admin.execute(sql)
        for sql in enable_tenant_rls_statements("notifications_inapp"):
            await admin.execute(sql)
        await _seed(admin, user)
        app_url = make_url(_url()).set(
            username=ROLE, password=PASSWORD, host="127.0.0.1", port=pg_proxy.port
        )
        await stack.enter_async_context(
            override_instance(
                core,
                "database",
                driver="sqlalchemy",
                url=app_url.render_as_string(hide_password=False),
                tenant_rls=True,
            )
        )
        await stack.enter_async_context(
            override_instance(
                core, "cache", driver="redis", url=os.environ["CACHE_URL"]
            )
        )
        await cache().delete(f"links:url:{LINK_CODE}")
        h = Hot(
            app=app,
            core=core,
            routes=routes,
            proxy=pg_proxy,
            admin=admin,
            redis_log=redis_log,
            executor=request.param,
            user=user,
            member=_bearer(_user_token(user)),
        )
        # warm both sides: pool, plan caches, metric children, type introspection
        for path, hdrs in _warm_cases(h):
            await h.both(path, hdrs)
        await cache().delete(f"links:url:{LINK_CODE}")
        try:
            yield h
        finally:
            h.use(True)


def _warm_cases(h: Hot) -> list[tuple[str, dict[str, str] | None]]:
    note = _bearer(_user_token(h.user, tenant_id=str(ORG)))
    return [
        ("/auth/me", h.member),
        (f"/api/v1/orgs/{ORG}/tickets", h.member),
        (f"/api/v1/orgs/{ORG}/tickets/export", h.member),
        ("/api/v1/notifications", note),
        (f"/api/v1/links/{LINK_CODE}", None),
    ]


# --------------------------------------------------------------------------- #
# The cases
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("first", ["old", "new"])
async def test_auth_me(hot: Hot, first: str) -> None:
    ok = await hot.check("me", "/auth/me", hot.member, first=first)
    assert ok.status == 200 and b"hot.reads@example.com" in ok.body.lower()
    # UserOut.email is a plain ``str`` on output (no EmailStr re-validation,
    # which lower-cased the domain): the stored value is returned as stored.
    assert b'"email":"Hot.Reads@Example.COM"' in ok.body
    a = await hot.check("me_anonymous", "/auth/me", None, first=first)
    assert a.status == 401
    a = await hot.check("me_garbage", "/auth/me", _bearer("nope"), first=first)
    assert a.status == 401
    stranger = _bearer(_user_token(uuid.uuid4()))
    a = await hot.check("me_unknown_user", "/auth/me", stranger, first=first)
    assert a.status == 404 and b"User not found" in a.body
    expired = _user_token(
        hot.user,
        iat=datetime.now(UTC) - timedelta(hours=2),
        nbf=datetime.now(UTC) - timedelta(hours=2),
        exp=datetime.now(UTC) - timedelta(hours=1),
    )
    a = await hot.check("me_expired", "/auth/me", _bearer(expired), first=first)
    assert a.status == 401
    refresh = _user_token(hot.user, token_type="refresh")
    a = await hot.check("me_refresh_token", "/auth/me", _bearer(refresh), first=first)
    assert a.status == 401


@pytest.mark.parametrize("first", ["old", "new"])
async def test_ticket_lists(hot: Hot, first: str) -> None:
    base = f"/api/v1/orgs/{ORG}/tickets"
    ok = await hot.check("tickets", base, hot.member, first=first)
    assert ok.status == 200 and ok.body.count(b'"subject"') == N_TICKETS
    a = await hot.check("tickets_limit", base + "?limit=3", hot.member, first=first)
    assert a.status == 200 and a.body.count(b'"subject"') == 3
    for bad in ("0", "101", "x"):
        a = await hot.check(
            f"tickets_bad_limit_{bad}", f"{base}?limit={bad}", hot.member, first=first
        )
        assert a.status == 422
    foreign = f"/api/v1/orgs/{OTHER_ORG}/tickets"
    a = await hot.check("tickets_foreign_org", foreign, hot.member, first=first)
    assert a.status == 403
    a = await hot.check("tickets_anonymous", base, None, first=first)
    assert a.status == 401
    a = await hot.check(
        "tickets_bad_org", "/api/v1/orgs/not-a-uuid/tickets", hot.member, first=first
    )
    assert a.status == 422
    # the export gate is the ORGANIZATION's live subscription (spec §7):
    # billing's facts read in its own contract unit, on both sides
    a = await hot.check(
        "export", base + "/export", hot.member, first=first, contract_units=2
    )
    assert a.status == 200 and b'"count":7' in a.body
    # the organization stops paying (the member's own claims never matter)
    await hot.admin.execute(
        "UPDATE billing_subscriptions SET status = 'canceled' WHERE customer_id = $1",
        ORG_CUSTOMER,
    )
    try:
        a = await hot.check(
            "export_unpaid", base + "/export", hot.member, first=first, contract_units=1
        )
        assert a.status == 402
    finally:
        await hot.admin.execute(
            "UPDATE billing_subscriptions SET status = 'active' WHERE customer_id = $1",
            ORG_CUSTOMER,
        )
    a = await hot.check("export_foreign", foreign + "/export", hot.member, first=first)
    assert a.status == 403


@pytest.mark.parametrize("first", ["old", "new"])
async def test_notifications(hot: Hot, first: str) -> None:
    path = "/api/v1/notifications"
    own = _bearer(_user_token(hot.user, tenant_id=str(ORG)))
    ok = await hot.check("notes", path, own, first=first, tenant=ORG)
    assert ok.status == 200 and ok.body.count(b'"kind"') == N_NOTES
    # the same user selecting the other tenant it is allowed: only ITS rows
    both = _bearer(
        _user_token(hot.user, tenant_ids=[str(ORG), str(OTHER_ORG)], tenant_id=None)
    )
    a = await hot.check(
        "notes_selected_b",
        path,
        {**both, "X-Tenant-Id": str(OTHER_ORG)},
        first=first,
        tenant=OTHER_ORG,
    )
    assert a.status == 200 and a.body.count(b'"kind"') == N_NOTES
    assert a.body != ok.body
    # a foreign tenant header outside the signed allowlist
    a = await hot.check(
        "notes_foreign_header",
        path,
        {**own, "X-Tenant-Id": str(OTHER_ORG)},
        first=first,
    )
    assert a.status == 403
    no_tenant = _bearer(_user_token(hot.user, tenant_ids=None))
    a = await hot.check("notes_no_tenant", path, no_tenant, first=first)
    assert a.status == 400
    # module_router authenticates first: no token is a 401 (not the tenant 400)
    a = await hot.check("notes_anonymous", path, None, first=first)
    assert a.status == 401
    empty = _bearer(_user_token(hot.user, tenant_id=str(uuid.uuid4())))
    a = await hot.check("notes_empty_tenant", path, empty, first=first, tenant=None)
    assert a.status == 200 and a.body == b"[]"


@pytest.mark.parametrize("first", ["old", "new"])
async def test_link_redirect(hot: Hot, first: str) -> None:
    path = f"/api/v1/links/{LINK_CODE}"
    # cache MISS on both sides: each side starts with the URL key deleted
    old_new: dict[str, Observation] = {}
    for side in ["old", "new"] if first == "old" else ["new", "old"]:
        await cache().delete(f"links:url:{LINK_CODE}")
        hot.use(side == "new")
        old_new[side] = await hot.call(path, None)
    hot.use(True)
    assert_same(old_new["old"], old_new["new"], "link_miss")
    assert_sql(old_new["old"].sql, old_new["new"].sql, "link_miss", tenant=None)
    assert old_new["old"].status == 307
    assert any(c[0] == "SET" for c in old_new["new"].redis)
    # cache HIT: no SQL at all on either side
    a = await hot.check("link_hit", path, None, first=first)
    assert a.status == 307 and a.sql == []
    a = await hot.check("link_unknown", "/api/v1/links/nosuch", None, first=first)
    assert a.status == 404


async def test_revoked_and_blocked_tokens(hot: Hot) -> None:
    from sandloom.core.auth.blocks import block_user
    from sandloom.core.testing import committing_uow

    sub = uuid.uuid4()
    old_tok = _user_token(
        sub, tenant_id=str(ORG), iat=datetime.now(UTC) - timedelta(seconds=30)
    )
    assert await revoke_user_tokens(str(sub))
    paths = (
        "/auth/me",
        f"/api/v1/orgs/{ORG}/tickets",
        f"/api/v1/orgs/{ORG}/tickets/export",
        "/api/v1/notifications",
    )
    for path in paths:
        a = await hot.check(f"revoked {path}", path, _bearer(old_tok))
        assert a.status == 401, (path, a.body)
    blocked = uuid.uuid4()
    sessions = async_sessionmaker(
        hot.core.database("default").engine, expire_on_commit=False
    )
    async with committing_uow(sessions):
        await block_user(blocked, reason="credential leak", actor_id=uuid.uuid4())
    tok = _bearer(_user_token(blocked, tenant_id=str(ORG)))
    for path in paths:
        a = await hot.check(f"blocked {path}", path, tok)
        # the kernel's live subject-record check answers first on every
        # authenticated route (ADR-0009), /auth/me included
        assert a.status == 401 and b"blocked" in a.body, (path, a.body)


async def test_revocation_store_down_fail_closed(
    hot: Hot, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis.asyncio.client as rclient

    async def down(self: Any, *args: Any, **options: Any) -> Any:
        hot.redis_log.append(tuple(str(a) for a in args))
        raise ConnectionError("redis down")

    monkeypatch.setattr(rclient.Redis, "execute_command", down)
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", True, raising=False)
    note = _bearer(_user_token(hot.user, tenant_id=str(ORG)))
    for path, hdrs in (
        ("/auth/me", hot.member),
        (f"/api/v1/orgs/{ORG}/tickets", hot.member),
        ("/api/v1/notifications", note),
    ):
        a = await hot.check(f"revocation_down {path}", path, hdrs)
        assert a.status == 503, (path, a.status)


@pytest.mark.parametrize(
    ("table", "path_of"),
    [
        ("users", lambda h: ("/auth/me", h.member)),
        ("tickets_tickets", lambda h: (f"/api/v1/orgs/{ORG}/tickets", h.member)),
        (
            "notifications_inapp",
            lambda h: (
                "/api/v1/notifications",
                _bearer(_user_token(h.user, tenant_id=str(ORG))),
            ),
        ),
        ("links_shortlinks", lambda h: (f"/api/v1/links/{LINK_CODE}", None)),
    ],
)
async def test_database_error(
    hot: Hot, table: str, path_of: Callable[[Hot], tuple[str, Any]]
) -> None:
    """The read fails in the database (privilege revoked): same answer, and no
    transaction, GUC or contextvar is left behind on either side."""
    path, hdrs = path_of(hot)
    await cache().delete(f"links:url:{LINK_CODE}")
    await hot.admin.execute(f"REVOKE SELECT ON {table} FROM {ROLE}")
    try:
        old, new = await hot.both(path, hdrs)
    finally:
        await hot.admin.execute(f"GRANT SELECT ON {table} TO {ROLE}")
    assert old.status == 500, (old.status, old.body)
    # the pooled connection may be replaced on either side (an executor
    # terminates a connection whose state is uncertain): compare its STATE
    old.pool, new.pool = old.pool[1:], new.pool[1:]
    assert_same(old, new, f"db_error {table}")
    assert old.pool == (False, ""), old.pool  # no open transaction, no GUC left


async def test_harness_detects_a_divergence(hot: Hot) -> None:
    """Negative control: a reference that skips the membership check (or reads
    another tenant) MUST show up as a divergence."""
    route, new_app, old_app = hot.routes[
        ("GET", "/api/v1/orgs/{organization_id}/tickets")
    ]
    foreign = f"/api/v1/orgs/{OTHER_ORG}/tickets"
    route.app = new_app
    b = await hot.call(foreign, hot.member)
    from helpdesk.modules.tickets.services import TicketService

    async def allow(*_a: Any, **_k: Any) -> None:
        return None

    real = TicketService._ensure_member
    TicketService._ensure_member = allow  # type: ignore[method-assign,assignment]
    try:
        route.app = old_app
        a = await hot.call(foreign, hot.member)
    finally:
        TicketService._ensure_member = real  # type: ignore[method-assign]
        route.app = new_app
    assert b.status == 403 and a.status == 200
    with pytest.raises(AssertionError, match="DIVERGENCE"):
        assert_same(a, b, "dropped_membership_check")


async def test_new_path_is_really_planned(hot: Hot) -> None:
    """Every ported operation runs on the executor of this parametrization —
    with the optimized executor on, none of them falls back to the ORM."""
    from helpdesk.modules.tickets.services.operations import tickets_of_org
    from linkly.modules.links.services.operations import link_target_by_code
    from projecthub.modules.notifications.services.operations import (
        recent_notifications,
    )

    from sandloom.core.planner import plan_for
    from sandloom_identity.services.operations import user_by_id

    expect = "optimized" if hot.executor == "planopt" else "general"
    for op in (user_by_id, tickets_of_org, recent_notifications, link_target_by_code):
        plan = plan_for(hot.core.runtime_view(), op)
        assert plan.executor == expect, (op.name, plan.reason)
