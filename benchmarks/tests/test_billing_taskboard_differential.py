"""Differential harness: billing reads and the taskboard, ORM/UoW vs planner.

The harness of ``test_hot_reads_differential`` (same observation, same stand:
the S5 role through the counting proxy, ONE pooled connection, the platform
and example modules in one app, both executors ``planopt`` / ``plan``) for the
three routes moved onto declared reads afterwards:

* ``GET /api/v1/billing/subscription`` and ``GET /api/v1/billing/credits`` —
  the reference is the ``DbSession`` handler of ``ef10ea3b`` (before the
  port), copied below: customer LEFT JOIN subscriptions / balance, and a
  customer CREATED when the tenant has none;
* ``GET /api/v1/taskboard/tasks`` (projecthub, ``TenantScope``) — the
  reference is the ``TaskboardQueryService.find`` handler of ``ef10ea3b``.

Compared byte for byte per case (``Observation``): status, body, the raw
header list, escaped exception, Redis trace, Prometheus deltas, outbox / audit
deltas, the pooled connection's state, idle-in-transaction sessions, leaked
contextvars. Asserted on the PostgreSQL trace: the same tables (FROM and JOIN)
read by the same number of data SELECTs, no DML, every transaction the port
added ``READ ONLY`` and none writable, the tenant GUC set for the taskboard.

Intended differences, asserted as such (never silently):

* billing, tenant WITHOUT a customer: the old path INSERTed the customer and
  its balance row; the new one writes nothing — and answers the SAME bytes;
* taskboard: ``limit=0`` / a negative limit no longer reach the database
  (same answers: ``[]`` / the masked 500); a prefix with ``%`` / ``_`` is now
  matched literally (it was a wildcard pattern).

Run (own DB / Redis index)::

    PYTHONPATH=../benchmarks:src uv run pytest -c pyproject.toml \\
        ../benchmarks/tests/test_billing_taskboard_differential.py -q --no-cov
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Depends, Query
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_hot_reads_differential import (
    Hot,
    Observation,
    _bearer,
    _examples_on_path,
    _user_token,
    assert_same,
)
from test_s5_secure_read import (  # noqa: F401 - fixtures
    PASSWORD,
    ROLE,
    _dsn,
    _url,
    pg_proxy,
)
from tests.perf_budgets.pgcount import PgCountingProxy

from sandloom.core.auth.revocation import revoke_user_tokens
from sandloom.core.config.settings import settings
from sandloom.core.di import DbSession, TenantScope
from sandloom.core.route_markers import prune_marker_dependencies
from sandloom.core.testing.infra import override_instance
from sandloom.shared.guards.auth import get_current_user_id
from benchsuite import mix
from benchsuite.setup_db import s5_statements

pytestmark = pytest.mark.asyncio

SUB = "/api/v1/billing/subscription"
CREDITS = "/api/v1/billing/credits"
BOARD = "/api/v1/taskboard/tasks"

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000c1")  # U_ORG owns it
OTHER_ORG = uuid.UUID("00000000-0000-4000-8000-0000000000c2")
NEW_ORG = uuid.UUID("00000000-0000-4000-8000-0000000000c3")  # no customer
U_PERSONAL = uuid.UUID("00000000-0000-4000-8000-00000000d001")
U_ORG = uuid.UUID("00000000-0000-4000-8000-00000000d002")
U_TWO = uuid.UUID("00000000-0000-4000-8000-00000000d003")  # ORG + OTHER_ORG
U_NONE = uuid.UUID("00000000-0000-4000-8000-00000000d004")  # no customer
U_NEW_ORG = uuid.UUID("00000000-0000-4000-8000-00000000d005")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
#: projection rows of ORG: (title, task status, project, project status)
BOARD_ROWS = [
    ("t0", "active", "Dev board", "active"),
    ("t1", "done", "dev tools", "active"),
    ("t2", "active", "DEVOPS", "archived"),
    ("t3", "active", "Design", "active"),
    ("t4", "todo", "Marketing", "active"),
    ("t5", "active", "Dxv lab", "active"),
    ("t6", "active", "D_v lab", "active"),
    ("t7", "active", "100% done", "active"),
]


# --------------------------------------------------------------------------- #
# Reference implementations: the handlers BEFORE the port (ef10ea3b), on the
# request Unit of Work.
# --------------------------------------------------------------------------- #
def _reference_handlers() -> dict[tuple[str, str], Callable[..., Any]]:
    from projecthub.modules.taskboard.schemas import TaskboardRowOut
    from projecthub.modules.taskboard.services import TaskboardQueryService

    from sandloom_billing.models import BillingCustomer, CreditBalance
    from sandloom_billing.schemas import CreditsOut, SubscriptionOut
    from sandloom_billing.services import BillingService
    from sandloom_billing.services.tenant_resolver import resolve_tenant
    from sandloom.core.di import CoreRuntime, get_core
    from sandloom.shared.authz import current_context
    from sandloom.shared.subject import UserContext

    async def tenant_subscription_status(
        tenant_type: str, tenant_id: UUID, db: Any
    ) -> SubscriptionOut:
        subs = await BillingService.tenant_subscriptions(tenant_type, tenant_id, db)
        if subs is None:
            customer = await BillingService._ensure_tenant_customer(
                tenant_type, tenant_id, db
            )
            return await BillingService.get_subscription_status(customer.id, db)
        return await BillingService._status_of(subs, db)

    async def tenant_credit_balance(tenant_type: str, tenant_id: UUID, db: Any) -> int:
        result = await db.execute(
            select(BillingCustomer.id, CreditBalance.amount)
            .select_from(BillingCustomer)
            .outerjoin(CreditBalance, CreditBalance.customer_id == BillingCustomer.id)
            .where(BillingService._tenant_match(tenant_type, tenant_id))
        )
        row = result.one_or_none()
        if row is None:
            customer = await BillingService._ensure_tenant_customer(
                tenant_type, tenant_id, db
            )
            return await BillingService.get_credit_balance(customer.id, db)
        return int(row.amount or 0)

    async def get_subscription(
        db: DbSession,
        org_id: UUID | None = Query(default=None),
        user_id: UUID = Depends(get_current_user_id),
        subject: UserContext = Depends(current_context),
        core: CoreRuntime = Depends(get_core),
    ) -> SubscriptionOut:
        tenant = await resolve_tenant(
            core=core, user_id=user_id, org_id=org_id, db=db, subject=subject
        )
        return await tenant_subscription_status(
            tenant.tenant_type, tenant.tenant_id, db
        )

    async def get_credits(
        db: DbSession,
        org_id: UUID | None = Query(default=None),
        user_id: UUID = Depends(get_current_user_id),
        subject: UserContext = Depends(current_context),
        core: CoreRuntime = Depends(get_core),
    ) -> CreditsOut:
        tenant = await resolve_tenant(
            core=core, user_id=user_id, org_id=org_id, db=db, subject=subject
        )
        balance = await tenant_credit_balance(tenant.tenant_type, tenant.tenant_id, db)
        return CreditsOut.of(balance)

    async def list_tasks(
        db: DbSession,
        tenant_id: UUID = TenantScope(),
        task_status: str | None = Query(default=None),
        project_status: str | None = Query(default=None),
        project_prefix: str | None = Query(default=None),
        limit: int = Query(default=50, le=200),
        _user_id: UUID = Depends(get_current_user_id),
    ) -> list[TaskboardRowOut]:
        rows = await TaskboardQueryService.find(
            db,
            tenant_id=tenant_id,
            task_status=task_status,
            project_status=project_status,
            project_prefix=project_prefix,
            limit=limit,
        )
        return [TaskboardRowOut.model_validate(r) for r in rows]

    return {
        ("GET", SUB): get_subscription,
        ("GET", CREDITS): get_credits,
        ("GET", BOARD): list_tasks,
    }


# --------------------------------------------------------------------------- #
# The SQL assertion (FROM and JOIN tables: the port reads joins from the
# other side, e.g. subscriptions JOIN customers for customers LEFT JOIN ...)
# --------------------------------------------------------------------------- #
_TABLE = re.compile(r'\b(?:FROM|JOIN)\s+"?([a-z_]+)"?', re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def _statements(sql: list[str]) -> list[str]:
    return [s.strip() for rt in sql for s in rt.split(" | ")]


def _data_reads(sql: list[str]) -> list[tuple[str, ...]]:
    """The table SET of each data SELECT, in order (driver introspection and
    the tenant ``set_config`` excluded)."""
    out = []
    for stmt in _statements(sql):
        up = stmt.upper()
        if not up.startswith("SELECT") or "SET_CONFIG" in up:
            continue
        if "PG_CATALOG" in up or "PG_TYPE" in up or "CURRENT_SCHEMA()" in up:
            continue  # driver introspection of a newly opened connection
        out.append(tuple(sorted(set(_TABLE.findall(stmt)))))
    return out


def _dml(sql: list[str]) -> list[str]:
    return [s for s in _statements(sql) if _DML.match(s)]


def _begins(sql: list[str]) -> Counter[str]:
    """The data transactions' BEGINs (the dialect's first-connect
    initialisation of a newly opened connection — ``BEGIN; select
    pg_catalog.version() ... ROLLBACK`` — is not a data transaction)."""
    stmts = _statements(sql)
    return Counter(
        s
        for i, s in enumerate(stmts)
        if s.upper().startswith("BEGIN")
        and not (
            i + 1 < len(stmts)
            and stmts[i + 1].lower().startswith("select pg_catalog.version()")
        )
    )


def assert_read_only_port(
    old: list[str], new: list[str], case: str, *, tenant: Any = None
) -> None:
    """The declared path reads what the UoW path read, read-only, no DML."""
    assert Counter(_data_reads(old)) == Counter(_data_reads(new)), (case, old, new)
    assert _dml(new) == [], (case, new)
    assert_read_only(old, new, case, tenant=tenant)


def assert_read_only(
    old: list[str], new: list[str], case: str, *, tenant: Any = None
) -> None:
    assert _dml(new) == [], (case, new)
    if not old:
        assert new == [], (case, new)
        return
    b_old, b_new = _begins(old), _begins(new)
    added = b_new - b_old
    assert all("READ ONLY" in s.upper() for s in added), (case, added)
    writable = sum(n for s, n in b_new.items() if "READ ONLY" not in s.upper())
    assert writable == 0, (case, b_old, b_new)
    if tenant is not None:
        assert any("set_config" in rt for rt in new), (case, new)
        if any("READ ONLY; SELECT set_config" in rt for rt in new):
            assert any(str(tenant) in rt for rt in new), (case, new)


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
async def _seed(admin: asyncpg.Connection) -> None:
    await admin.execute(
        "TRUNCATE billing_customers, billing_prices, billing_products, "
        "taskboard_project_task_query_index CASCADE"
    )
    orgs = (
        (ORG, U_ORG, [U_ORG, U_TWO]),
        (OTHER_ORG, uuid.uuid4(), [U_TWO]),
        (NEW_ORG, U_NEW_ORG, [U_NEW_ORG]),
    )
    for org, owner, members in orgs:
        await admin.execute(
            """INSERT INTO tenancy_organizations
                   (id, name, slug, owner_id, is_active, created_at, updated_at)
               VALUES ($1, 'Billing Org', $2, $3, true, now(), now())""",
            org,
            f"bt-{org.hex[-12:]}",
            owner,
        )
        for user in dict.fromkeys([owner, *members]):
            await admin.execute(
                """INSERT INTO tenancy_members (id, organization_id, user_id, role,
                       created_at) VALUES ($1, $2, $3, $4, now())""",
                uuid.uuid4(),
                org,
                user,
                "owner" if user == owner else "member",
            )
    await admin.execute(
        "INSERT INTO billing_products (id, name, type, is_active) VALUES"
        " (901, 'Pro', 'subscription', true), (902, 'Team', 'subscription', true)"
    )
    await admin.execute(
        "INSERT INTO billing_prices (id, product_id, amount, currency,"
        " recurring_interval, stripe_price_id) VALUES"
        " (911, 901, 9900, 'USD', 'month', 'price_bt_pro'),"
        " (912, 902, 29900, 'USD', 'year', 'price_bt_team')"
    )
    c_personal, c_org, c_other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin.execute(
        "INSERT INTO billing_customers (id, user_id, organization_id) VALUES"
        " ($1, $2, NULL), ($3, NULL, $4), ($5, NULL, $6)",
        c_personal,
        U_PERSONAL,
        c_org,
        ORG,
        c_other,
        OTHER_ORG,
    )
    # personal: 42 credits; an older canceled and a newer active subscription
    # org: NO balance row (the LEFT JOIN's NULL -> 0); a trialing subscription
    await admin.execute(
        "INSERT INTO billing_credit_balances (customer_id, amount) VALUES"
        " ($1, 42), ($2, 7)",
        c_personal,
        c_other,
    )
    now = datetime.now(UTC)
    await admin.executemany(
        "INSERT INTO billing_subscriptions (customer_id, price_id, status,"
        " current_period_end, trial_end, cancel_at_period_end, origin)"
        " VALUES ($1, $2, $3, $4, $5, $6, 'intent')",
        [
            (c_personal, 911, "canceled", now - timedelta(days=40), None, False),
            (c_personal, 911, "active", now + timedelta(days=20), None, True),
            (c_org, 912, "trialing", None, now + timedelta(days=5), False),
            (c_other, 911, "active", now + timedelta(days=3), None, False),
        ],
    )
    await admin.executemany(
        "INSERT INTO taskboard_project_task_query_index (task_id, tenant_id,"
        " task_title, task_status, task_version, project_id, project_name,"
        " project_status, project_version, updated_at)"
        " VALUES ($1, $2, $3, $4, 1, $5, $6, $7, 1, $8)",
        [
            (
                uuid.uuid4(),
                tenant,
                t,
                ts,
                uuid.uuid4(),
                p,
                ps,
                T0 + timedelta(minutes=i),
            )
            for tenant in (ORG, OTHER_ORG)
            for i, (t, ts, p, ps) in enumerate(BOARD_ROWS)
        ],
    )


async def _drop_customers(admin: asyncpg.Connection) -> None:
    """Undo what the OLD path created for the no-customer tenants."""
    await admin.execute(
        "DELETE FROM billing_customers WHERE user_id = $1 OR organization_id = $2",
        U_NONE,
        NEW_ORG,
    )


async def _customers_of_new_tenants(admin: asyncpg.Connection) -> int:
    return int(
        await admin.fetchval(
            "SELECT count(*) FROM billing_customers"
            " WHERE user_id = $1 OR organization_id = $2",
            U_NONE,
            NEW_ORG,
        )
    )


def _token(user: UUID, **over: Any) -> dict[str, str]:
    return _bearer(_user_token(user, **over))


def _board_token(**over: Any) -> dict[str, str]:
    return _token(U_ORG, tenant_id=str(ORG), **over)


@pytest_asyncio.fixture(params=["planopt", "plan"])
async def bt(
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
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(open_test_engine())
        admin = await asyncpg.connect(_dsn(make_url(_url())))
        stack.push_async_callback(admin.close)
        await admin.execute("DROP TABLE IF EXISTS bench_tenant_items CASCADE")
        for sql in s5_statements(ROLE, PASSWORD):
            await admin.execute(sql)
        await _seed(admin)
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
        h = Hot(
            app=app,
            core=core,
            routes=routes,
            proxy=pg_proxy,
            admin=admin,
            redis_log=redis_log,
            executor=request.param,
            user=U_PERSONAL,
            member=_token(U_PERSONAL),
        )
        # warm both sides: pool, plan caches, metric children, introspection
        for path, hdrs in (
            (SUB, h.member),
            (CREDITS, h.member),
            (SUB, _token(U_ORG)),
            (CREDITS, _token(U_ORG)),
            (BOARD, _board_token()),
            (BOARD + "?task_status=active&project_prefix=d", _board_token()),
        ):
            await h.both(path, hdrs)
        try:
            yield h
        finally:
            h.use(True)


async def check(
    h: Hot, name: str, path: str, hdrs: Any, *, first: str, tenant: Any = None
) -> Observation:
    old, new = await h.both(path, hdrs, first=first)
    assert_same(old, new, name)
    assert_read_only_port(old.sql, new.sql, name, tenant=tenant)
    return old


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("first", ["old", "new"])
@pytest.mark.parametrize("path", [SUB, CREDITS])
async def test_billing_reads(bt: Hot, first: str, path: str) -> None:
    is_sub = path == SUB
    a = await check(bt, "personal", path, bt.member, first=first)
    assert a.status == 200
    if is_sub:
        assert b'"status":"active"' in a.body and b'"name":"Pro"' in a.body
        assert b'"cancel_at_period_end":true' in a.body
    else:
        assert a.body == b'{"balance":42,"debt":0,"in_debt":false}'
    org = _token(U_ORG)
    # Without org_id the account is the caller's OWN (spec §7.4: the
    # single-organization inference is gone): the member has no personal
    # customer, so the answer is the empty personal state.
    a = await check(bt, "org_member_personal", path, org, first=first)
    assert a.status == 200
    if is_sub:
        assert b'"status":"none"' in a.body and b'"plan":null' in a.body
    else:
        assert a.body == b'{"balance":0,"debt":0,"in_debt":false}'
    b = await check(bt, "org_explicit", f"{path}?org_id={ORG}", org, first=first)
    assert b.status == 200
    if is_sub:
        assert b'"status":"trialing"' in b.body and b'"name":"Team"' in b.body
    else:  # a customer WITHOUT a balance row
        assert b.body == b'{"balance":0,"debt":0,"in_debt":false}'
    for name, query, status in (
        ("foreign_org", f"?org_id={OTHER_ORG}", 403),
        ("unknown_org", f"?org_id={uuid.uuid4()}", 403),
        ("bad_org_id", "?org_id=nope", 422),
    ):
        a = await check(bt, name, path + query, org, first=first)
        assert a.status == status, (name, a.body)
    # A member of two organizations without org_id: no longer ambiguous (422)
    # but the caller's own personal account (spec §7.4) — which has no
    # customer, so only the new side runs (the old reference CREATES the
    # customer: the intended difference of the no-customer test below).
    bt.use(True)
    a = await bt.call(path, _token(U_TWO))
    assert a.status == 200, a.body
    assert _dml(a.sql) == [], a.sql
    if is_sub:
        assert b'"status":"none"' in a.body
    else:
        assert a.body == b'{"balance":0,"debt":0,"in_debt":false}'
    for name, hdrs in (
        ("anonymous", None),
        ("garbage", _bearer("nope")),
        ("refresh_token", _token(U_PERSONAL, token_type="refresh")),
    ):
        a = await check(bt, name, path, hdrs, first=first)
        assert a.status == 401, (name, a.body)


@pytest.mark.parametrize("first", ["old", "new"])
@pytest.mark.parametrize("path", [SUB, CREDITS])
@pytest.mark.parametrize(
    ("tenant", "user", "query"),
    [("personal", U_NONE, ""), ("org", U_NEW_ORG, f"?org_id={NEW_ORG}")],
)
async def test_tenant_without_customer_answers_the_same_and_writes_nothing(
    bt: Hot, first: str, path: str, tenant: str, user: UUID, query: str
) -> None:
    """The old path created the customer (INSERT customer + balance) and
    answered the fresh customer's state; the new one answers the SAME bytes
    and writes nothing. Each side starts with no customer."""
    hdrs = _token(user)
    seen: dict[str, Observation] = {}
    for side in ["old", "new"] if first == "old" else ["new", "old"]:
        await _drop_customers(bt.admin)
        bt.use(side == "new")
        seen[side] = await bt.call(path + query, hdrs)
        created = await _customers_of_new_tenants(bt.admin)
        assert created == (1 if side == "old" else 0), (side, created)
    bt.use(True)
    await _drop_customers(bt.admin)
    old, new = seen["old"], seen["new"]
    assert_same(old, new, f"no_customer {tenant} {path}")
    assert old.status == 200
    assert old.body == (
        b'{"status":"none","current_period_end":null,"trial_end":null,'
        b'"cancel_at_period_end":false,"plan":null}'
        if path == SUB
        else b'{"balance":0,"debt":0,"in_debt":false}'
    )
    # the old path wrote (customer + balance row); the new one is read-only
    assert any("billing_customers" in s for s in _dml(old.sql)), old.sql
    assert any("billing_credit_balances" in s for s in _dml(old.sql)), old.sql
    assert_read_only(old.sql, new.sql, f"no_customer {tenant} {path}")
    tables = {t for read in _data_reads(new.sql) for t in read}
    assert "billing_customers" in tables, new.sql


# --------------------------------------------------------------------------- #
# Taskboard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("first", ["old", "new"])
async def test_taskboard(bt: Hot, first: str) -> None:
    own = _board_token()
    titles = [t for t, *_ in reversed(BOARD_ROWS)]
    a = await check(bt, "board", BOARD, own, first=first, tenant=ORG)
    assert a.status == 200 and a.body.count(b'"task_title"') == len(BOARD_ROWS)
    assert a.body.index(titles[0].encode()) < a.body.index(titles[-1].encode())
    for name, query, n in (
        ("task_status", "?task_status=active", 6),
        ("project_status", "?project_status=archived", 1),
        ("both_statuses", "?task_status=active&project_status=active", 5),
        ("prefix_ci", "?project_prefix=dev", 3),
        ("prefix_upper", "?project_prefix=DE", 4),
        ("prefix_and_status", "?task_status=active&project_prefix=d", 5),
        ("prefix_empty", "?project_prefix=", 8),
        ("prefix_none_match", "?project_prefix=zz", 0),
        ("no_match_status", "?task_status=nosuch", 0),
        ("limit", "?limit=3", 3),
        ("limit_cap", "?limit=200", 8),
        ("all_filters_limit", "?task_status=active&project_prefix=D&limit=2", 2),
    ):
        a = await check(bt, name, BOARD + query, own, first=first, tenant=ORG)
        assert a.status == 200 and a.body.count(b'"task_title"') == n, (name, a.body)
    for name, query in (("limit_over", "?limit=201"), ("limit_nan", "?limit=x")):
        a = await check(bt, name, BOARD + query, own, first=first, tenant=ORG)
        assert a.status == 422
    # the selector inside / outside the signed allowlist; no tenant; no token
    both = _token(U_ORG, tenant_ids=[str(ORG), str(OTHER_ORG)], tenant_id=None)
    a = await check(
        bt,
        "selected_other",
        BOARD,
        {**both, "X-Tenant-Id": str(OTHER_ORG)},
        first=first,
        tenant=OTHER_ORG,
    )
    assert a.status == 200 and a.body.count(b'"task_title"') == len(BOARD_ROWS)
    a = await check(
        bt, "foreign_header", BOARD, {**own, "X-Tenant-Id": str(OTHER_ORG)}, first=first
    )
    assert a.status == 403
    a = await check(bt, "no_tenant", BOARD, _token(U_ORG, tenant_ids=None), first=first)
    assert a.status == 400
    a = await check(bt, "anonymous", BOARD, None, first=first)
    assert a.status == 401  # module_router authenticates before it binds the tenant
    empty = _token(U_ORG, tenant_id=str(uuid.uuid4()))
    a = await check(bt, "empty_tenant", BOARD, empty, first=first)
    assert a.status == 200 and a.body == b"[]"


@pytest.mark.parametrize("first", ["old", "new"])
async def test_taskboard_limits_that_no_longer_reach_the_database(
    bt: Hot, first: str
) -> None:
    """``limit=0`` -> ``[]`` and a negative limit -> the masked 500, as the
    database answered them; the new path sends no SQL for either."""
    for name, query, status in (("zero", "?limit=0", 200), ("neg", "?limit=-1", 500)):
        old, new = await bt.both(BOARD + query, _board_token(), first=first)
        assert old.status == status, (name, old.body)
        old.pool, new.pool = old.pool[1:], new.pool[1:]  # (in tx?, GUC) only
        assert_same(old, new, f"limit_{name}")
        assert new.sql == [] and _dml(old.sql) == [], (name, old.sql, new.sql)


async def test_taskboard_wildcard_prefix_is_now_literal(bt: Hot) -> None:
    """Intended difference: ``_`` / ``%`` in the prefix are literal now (the
    ORM's ``ILIKE 'D_v%'`` also matched "Dev ..." and "Dxv ...")."""
    for query, old_titles, new_titles in (
        ("?project_prefix=D_v", {b"t0", b"t1", b"t2", b"t5", b"t6"}, {b"t6"}),
        ("?project_prefix=100%25", {b"t7"}, {b"t7"}),
    ):
        old, new = await bt.both(BOARD + query, _board_token())
        assert old.status == new.status == 200
        found = re.compile(rb'"task_title":"(t\d)"')
        assert set(found.findall(old.body)) == old_titles, old.body
        assert set(found.findall(new.body)) == new_titles, new.body
        assert_read_only(old.sql, new.sql, query, tenant=ORG)


# --------------------------------------------------------------------------- #
# Every route: revoked / blocked tokens, revocation store down, DB errors
# --------------------------------------------------------------------------- #
async def test_revoked_and_blocked_tokens(bt: Hot) -> None:
    from sandloom.core.auth.blocks import block_user
    from sandloom.core.testing import committing_uow

    sub = uuid.uuid4()
    old_tok = _token(
        sub, tenant_id=str(ORG), iat=datetime.now(UTC) - timedelta(seconds=30)
    )
    assert await revoke_user_tokens(str(sub))
    for path in (SUB, CREDITS, BOARD):
        a = await check(bt, f"revoked {path}", path, old_tok, first="old")
        assert a.status == 401, (path, a.body)
    blocked = uuid.uuid4()
    sessions = async_sessionmaker(
        bt.core.database("default").engine, expire_on_commit=False
    )
    async with committing_uow(sessions):
        await block_user(blocked, reason="credential leak", actor_id=uuid.uuid4())
    tok = _token(blocked, tenant_id=str(ORG))
    for path in (SUB, CREDITS, BOARD):
        a = await check(bt, f"blocked {path}", path, tok, first="new")
        assert a.status == 401 and b"blocked" in a.body, (path, a.body)


async def test_revocation_store_down_fail_closed(
    bt: Hot, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis.asyncio.client as rclient

    async def down(self: Any, *args: Any, **options: Any) -> Any:
        bt.redis_log.append(tuple(str(a) for a in args))
        raise ConnectionError("redis down")

    monkeypatch.setattr(rclient.Redis, "execute_command", down)
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", True, raising=False)
    for path, hdrs in ((SUB, bt.member), (CREDITS, bt.member), (BOARD, _board_token())):
        a = await check(bt, f"revocation_down {path}", path, hdrs, first="old")
        assert a.status == 503, (path, a.status)


@pytest.mark.parametrize(
    ("table", "path_of"),
    [
        ("billing_customers", lambda h: (SUB, h.member)),
        ("billing_subscriptions", lambda h: (SUB, h.member)),
        ("billing_prices", lambda h: (SUB, h.member)),
        ("billing_customers", lambda h: (CREDITS, h.member)),
        ("billing_credit_balances", lambda h: (CREDITS, h.member)),
        ("taskboard_project_task_query_index", lambda h: (BOARD, _board_token())),
    ],
)
async def test_database_error(
    bt: Hot, table: str, path_of: Callable[[Hot], tuple[str, Any]]
) -> None:
    """The read fails in the database (privilege revoked): same answer, and no
    transaction, GUC or contextvar is left behind on either side."""
    path, hdrs = path_of(bt)
    await bt.admin.execute(f"REVOKE SELECT ON {table} FROM {ROLE}")
    try:
        old, new = await bt.both(path, hdrs)
    finally:
        await bt.admin.execute(f"GRANT SELECT ON {table} TO {ROLE}")
    assert old.status == 500, (table, old.status, old.body)
    old.pool, new.pool = old.pool[1:], new.pool[1:]
    assert_same(old, new, f"db_error {table} {path}")
    assert old.pool == (False, ""), old.pool


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
async def test_harness_detects_a_divergence(bt: Hot) -> None:
    """Negative control: a reference that skips the tenant resolution (reads
    whatever organization the caller names) MUST diverge."""
    from sandloom_billing.schemas import CreditsOut
    from sandloom_billing.services import BillingService

    route, new_app, _old_app = bt.routes[("GET", CREDITS)]

    async def leaky(
        db: DbSession,
        org_id: UUID | None = Query(default=None),
        _user_id: UUID = Depends(get_current_user_id),
    ) -> CreditsOut:
        assert org_id is not None
        reads_customer = await BillingService.get_or_create_customer(
            db, organization_id=org_id
        )
        return CreditsOut.of(
            await BillingService.get_credit_balance(reads_customer.id, db)
        )

    bad = APIRoute(route.path, leaky, response_model=route.response_model)
    path = f"{CREDITS}?org_id={OTHER_ORG}"
    route.app = new_app
    good = await bt.call(path, _token(U_ORG))
    route.app = bad.app
    try:
        leaked = await bt.call(path, _token(U_ORG))
    finally:
        route.app = new_app
    assert good.status == 403 and leaked.status == 200, (good.body, leaked.body)
    with pytest.raises(AssertionError, match="DIVERGENCE"):
        assert_same(leaked, good, "dropped_tenant_resolution")


async def test_new_path_is_really_planned(bt: Hot) -> None:
    """Every ported operation runs on the executor of this parametrization."""
    from projecthub.modules.taskboard.services.operations import taskboard_rows

    from sandloom_billing.services.operations import OPERATIONS
    from sandloom.core.planner import plan_for

    expect = "optimized" if bt.executor == "planopt" else "general"
    for op in (*OPERATIONS, taskboard_rows):
        plan = plan_for(bt.core.runtime_view(), op)
        assert plan.executor == expect, (op.name, plan.reason)
