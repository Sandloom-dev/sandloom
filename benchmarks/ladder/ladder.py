"""Layered cost ladder for one indexed primary-key read (the S3 scenario).

Each rung adds exactly ONE layer on top of the previous one, all against the
same ``bench_items`` table seeded by ``benchsuite.setup_db`` and the same
deterministic sequence of random ids (1..9999):

    L0   asyncpg pool.acquire + fetchrow (statement cache = prepared)
    L0t  L0 inside an explicit transaction (BEGIN ... COMMIT)
    L1   L0 + orjson.dumps of a dict built from the Record
    L2*  SQLAlchemy Core on AsyncConnection
    L3*  SQLAlchemy ORM AsyncSession
    L4*  L3 + pydantic ItemOut + JSON bytes
    L5*  Sandloom engine / Sandloom db_session() Unit of Work
    L6*  full ASGI app driven in-process (Sandloom, bare FastAPI, fast-path probes)
    R*   DIAGNOSTIC: one contract call decomposed layer by layer (stands.py)
    L6W* DIAGNOSTIC: the write path without argon2, W1-W6 (stands.py)
    MIX  one request of the SaaS mix catalog, mono or a split service (mixrung.py)
    H*   the hot-path profiling stand's own rungs (hot_paths.py)
    WL*  the write / login profiling stand's own rungs (write_login.py)

Usage (from ``benchmarks/``; DATABASE_URL / CACHE_URL / BROKER_URL in env)::

    uv run python ladder/ladder.py list
    uv run python ladder/ladder.py cpu    L3 --n 5000 --reps 5 [--conc 64] [--loop uvloop]
    uv run python ladder/ladder.py alloc  L3 --n 2000
    uv run python ladder/ladder.py rt     L3 --n 200
    uv run python ladder/ladder.py calls  L3 --n 1000
    uv run python ladder/ladder.py layers HW7 --n 300   # CPU by layer (write_login)
    uv run --with memray python ladder/ladder.py memray L3 --n 1000
    python ladder/ladder.py cg L3 --n 200           # cachegrind workload (container)

Every invocation measures ONE rung in a fresh process and prints one JSON line,
so rungs never contaminate each other (e.g. Sandloom's tenant guard patches
``Session`` globally on import — rungs below L3g never import ``sandloom``).
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.util
import json
import os
import random
import resource
import statistics
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Script execution sets sys.path[0] to ladder/, not the benchmark package root.
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

# Sandloom rungs: production defaults exactly as the benchmark runner sets them,
# except the outbox relay (a background poller whose CPU would leak into the
# per-op numbers of an in-process loop; a read-only request never touches it).
_SANDLOOM_ENV = {
    "ENVIRONMENT": "prod",
    "SANDLOOM_MODULES": '["benchsuite.bench"]',
    "MODULE_REQUIRED": '["benchsuite.bench"]',
    "LOG_LEVEL": "WARNING",
    "RATE_LIMIT_ENABLED": "false",
    "OUTBOX_RELAY_ENABLED": "off",
    "JWT_SECRET": "bench-local-secret-0123456789abcdef0123456789abcdef",
    # prod without the token issuer (identity) on a shared HS256 secret, and
    # without an outbox relay
    "SANDLOOM_ACCEPTED_RISKS": "shared_hs_secret_split,relay_disabled",
}
for _k, _v in _SANDLOOM_ENV.items():
    os.environ.setdefault(_k, _v)

IDS = [random.Random(1234).randint(1, 9999) for _ in range(4096)]
Op = Callable[[int], Awaitable[Any]]


def _rid(i: int) -> int:
    return IDS[i & 4095]


# --------------------------------------------------------------------------- #
# Rungs. Each is ``async def setup() -> (op, teardown)``.
# --------------------------------------------------------------------------- #
RUNGS: dict[str, tuple[str, Callable[[], Awaitable[tuple[Op, Any]]]]] = {}


def rung(name: str, desc: str):
    def deco(fn):
        RUNGS[name] = (desc, fn)
        return fn

    return deco


SQL = "SELECT id, name, value FROM bench_items WHERE id = $1"


async def _noreset(_conn: Any) -> None:
    """Pool ``reset`` hook that sends nothing (the default sends
    ``pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL`` = +1 RT)."""


async def _pg_pool(*, reset: bool = False):
    import asyncpg

    from benchsuite import common

    kw: dict[str, Any] = {} if reset else {"reset": _noreset}
    return await asyncpg.create_pool(
        common.asyncpg_dsn(), min_size=10, max_size=30, **kw
    )


@rung("L0", "asyncpg pool.acquire + fetchrow -> Record (no tx, no-op pool reset)")
async def _l0():
    pool = await _pg_pool()

    async def op(i: int):
        async with pool.acquire() as c:
            return await c.fetchrow(SQL, _rid(i))

    return op, pool.close


@rung("L0r", "L0 with asyncpg's DEFAULT pool reset query on release (+1 RT)")
async def _l0r():
    pool = await _pg_pool(reset=True)

    async def op(i: int):
        async with pool.acquire() as c:
            return await c.fetchrow(SQL, _rid(i))

    return op, pool.close


@rung("L0t", "L0 inside an explicit transaction (BEGIN/COMMIT)")
async def _l0t():
    pool = await _pg_pool()

    async def op(i: int):
        async with pool.acquire() as c, c.transaction():
            return await c.fetchrow(SQL, _rid(i))

    return op, pool.close


_TENANT = "6f1c2a3e-0000-4000-8000-000000000001"


@rung(
    "L0s",
    "L0t + RLS tenant sync as Sandloom does it: BEGIN, set_config(..,true), SELECT, COMMIT",
)
async def _l0s():
    pool = await _pg_pool()

    async def op(i: int):
        async with pool.acquire() as c, c.transaction():
            await c.fetchrow(
                "SELECT set_config('sandloom.tenant_id', $1, true), "
                "set_config('sandloom.tenant_bypass', $2, true)",
                _TENANT,
                "",
            )
            return await c.fetchrow(SQL, _rid(i))

    return op, pool.close


@rung(
    "L0s1",
    "L0s with BEGIN + set_config merged into ONE simple query (validated UUID literal)",
)
async def _l0s1():
    import uuid

    pool = await _pg_pool()

    async def op(i: int):
        tenant = str(uuid.UUID(_TENANT))  # validated -> safe to inline
        async with pool.acquire() as c:
            await c.execute(
                "BEGIN; SELECT set_config('sandloom.tenant_id', '" + tenant + "', true), "
                "set_config('sandloom.tenant_bypass', '', true)"
            )
            try:
                row = await c.fetchrow(SQL, _rid(i))
            finally:
                await c.execute("COMMIT")
            return row

    return op, pool.close


@rung("L1", "L0 + orjson.dumps(dict from Record) -> bytes")
async def _l1():
    import orjson

    pool = await _pg_pool()

    async def op(i: int):
        async with pool.acquire() as c:
            r = await c.fetchrow(SQL, _rid(i))
        return orjson.dumps({"id": r[0], "name": r[1], "value": r[2]})

    return op, pool.close


def _bare():
    """benchsuite's bare SQLAlchemy setup, with a NON-CHURNING pool.

    The stock QueuePool (10 + 20 overflow) closes overflow connections on
    checkin and reconnects on the next shortage; at conc 64 that measured
    100-600 reconnects per 38k ops (~3.7 ms CPU each), inflating the bare
    rungs. Sandloom uses a lingering pool; for a like-for-like comparison the bare
    engine gets the same 30 connections as a fixed pool (30 + 0). Set
    LADDER_BARE_POOL=stock to measure the stock behaviour.
    """
    from benchsuite.bare import db

    if os.environ.get("LADDER_BARE_POOL", "fixed") == "fixed" and not getattr(
        db, "_ladder_fixed", False
    ):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from benchsuite import common

        db.engine = create_async_engine(
            common.database_url(),
            echo=False,
            pool_pre_ping=False,
            pool_use_lifo=True,
            pool_recycle=1800,
            pool_size=common.pool_size() + common.max_overflow(),
            max_overflow=0,
            pool_timeout=30,
        )
        db.SessionLocal = async_sessionmaker(db.engine, expire_on_commit=False)
        db._ladder_fixed = True
    return db


async def _l2w(hook: str, *, switch: bool = False):
    """ONE connection, ONE open transaction, a tenant bound; op = one prebuilt
    Core SELECT. Isolates the RLS hook's PER-STATEMENT cost (after the first
    statement the settings are in sync, so a correct hook only checks)."""
    import uuid

    from sqlalchemy import bindparam, select
    from sqlalchemy.ext.asyncio import create_async_engine

    from sandloom.core.tenant_context import _current_tenant
    from benchsuite import common

    engine = create_async_engine(common.database_url(), pool_size=1, max_overflow=0)
    if hook == "new":
        from sandloom.core.sql_alchemy.tenant_rls import install_tenant_rls

        install_tenant_rls(engine)
    elif hook == "old":
        import old_tenant_rls

        old_tenant_rls.install_tenant_rls(engine)
    db = _bare()
    t = db.Item.__table__
    stmt = select(t).where(t.c.id == bindparam("id"))
    tenants = [uuid.UUID(int=1), uuid.UUID(int=2)]
    _current_tenant.set(tenants[0])
    conn = await engine.connect()
    await conn.begin()

    async def op(i: int):
        if switch:  # force a set_config on every statement
            _current_tenant.set(tenants[i & 1])
        return (await conn.execute(stmt, {"id": _rid(i)})).first()

    async def teardown():
        await conn.rollback()
        await conn.close()
        await engine.dispose()

    return op, teardown


@rung("L2Wp", "1 conn/1 tx, prebuilt Core SELECT, tenant bound, NO RLS hook")
async def _l2wp():
    return await _l2w("none")


@rung("L2Wn", "L2Wp + the stage-1 dialect-wrapper RLS hook (in sync: check only)")
async def _l2wn():
    return await _l2w("new")


@rung("L2Wo", "L2Wp + the old before_cursor_execute RLS hook (in sync: check only)")
async def _l2wo():
    return await _l2w("old")


@rung("L2Wns", "L2Wn with the tenant switched every op (set_config every statement)")
async def _l2wns():
    return await _l2w("new", switch=True)


@rung("L2Wos", "L2Wo with the tenant switched every op (set_config every statement)")
async def _l2wos():
    return await _l2w("old", switch=True)


@rung("L2", "SQLAlchemy Core: engine.connect + select(table) built per op -> Row")
async def _l2():
    from sqlalchemy import select

    db = _bare()
    t = db.Item.__table__

    async def op(i: int):
        async with db.engine.connect() as conn:
            return (await conn.execute(select(t).where(t.c.id == _rid(i)))).first()

    return op, db.engine.dispose


@rung("L2c", "L2 with one prebuilt statement + bindparam (cache key memoized)")
async def _l2c():
    from sqlalchemy import bindparam, select

    db = _bare()
    t = db.Item.__table__
    stmt = select(t).where(t.c.id == bindparam("id"))

    async def op(i: int):
        async with db.engine.connect() as conn:
            return (await conn.execute(stmt, {"id": _rid(i)})).first()

    return op, db.engine.dispose


@rung("L2nc", "L2 with the compiled cache disabled (compile every op)")
async def _l2nc():
    from sqlalchemy import select

    db = _bare()
    t = db.Item.__table__
    eng = db.engine.execution_options(compiled_cache=None)

    async def op(i: int):
        async with eng.connect() as conn:
            return (await conn.execute(select(t).where(t.c.id == _rid(i)))).first()

    return op, db.engine.dispose


@rung("L2t", "L2 inside engine.begin() (BEGIN/COMMIT instead of BEGIN/ROLLBACK)")
async def _l2t():
    from sqlalchemy import select

    db = _bare()
    t = db.Item.__table__

    async def op(i: int):
        async with db.engine.begin() as conn:
            return (await conn.execute(select(t).where(t.c.id == _rid(i)))).first()

    return op, db.engine.dispose


@rung("L2a", "L2c on an AUTOCOMMIT connection (no BEGIN/ROLLBACK)")
async def _l2a():
    from sqlalchemy import bindparam, select

    db = _bare()
    t = db.Item.__table__
    stmt = select(t).where(t.c.id == bindparam("id"))
    eng = db.engine.execution_options(isolation_level="AUTOCOMMIT")

    async def op(i: int):
        async with eng.connect() as conn:
            return (await conn.execute(stmt, {"id": _rid(i)})).first()

    return op, db.engine.dispose


@rung("L2A", "L2c on a DEDICATED engine created with isolation_level=AUTOCOMMIT")
async def _l2A():
    from sqlalchemy import bindparam, select
    from sqlalchemy.ext.asyncio import create_async_engine

    from benchsuite import common

    db = _bare()
    t = db.Item.__table__
    stmt = select(t).where(t.c.id == bindparam("id"))
    eng = create_async_engine(
        common.database_url(),
        pool_pre_ping=False,
        pool_use_lifo=True,
        pool_size=common.pool_size(),
        max_overflow=common.max_overflow(),
        isolation_level="AUTOCOMMIT",
    )

    async def op(i: int):
        async with eng.connect() as conn:
            return (await conn.execute(stmt, {"id": _rid(i)})).first()

    return op, eng.dispose


@rung(
    "L2E",
    "L2c + three NO-OP engine event listeners (before/after_cursor_execute, handle_error)",
)
async def _l2e():
    from sqlalchemy import bindparam, event, select

    db = _bare()
    t = db.Item.__table__
    stmt = select(t).where(t.c.id == bindparam("id"))
    se = db.engine.sync_engine
    noop = lambda *a, **k: None  # noqa: E731
    event.listen(se, "before_cursor_execute", noop)
    event.listen(se, "after_cursor_execute", noop)
    event.listen(se, "handle_error", noop)

    async def op(i: int):
        async with db.engine.connect() as conn:
            return (await conn.execute(stmt, {"id": _rid(i)})).first()

    return op, db.engine.dispose


@rung("L3", "ORM: AsyncSession + session.get(Item) -> instance (bare FastAPI S3)")
async def _l3():
    db = _bare()

    async def op(i: int):
        async with db.SessionLocal() as s:
            return await s.get(db.Item, _rid(i))

    return op, db.engine.dispose


@rung("L3s", "ORM: session.execute(select(Item).where(id==)) .scalar_one_or_none")
async def _l3s():
    from sqlalchemy import select

    db = _bare()
    Item = db.Item

    async def op(i: int):
        async with db.SessionLocal() as s:
            return (
                await s.execute(select(Item).where(Item.id == _rid(i)))
            ).scalar_one_or_none()

    return op, db.engine.dispose


@rung("L3p", "L3s + execution_options(populate_existing=True)")
async def _l3p():
    from sqlalchemy import select

    db = _bare()
    Item = db.Item

    async def op(i: int):
        async with db.SessionLocal() as s:
            stmt = (
                select(Item)
                .where(Item.id == _rid(i))
                .execution_options(populate_existing=True)
            )
            return (await s.execute(stmt)).scalar_one_or_none()

    return op, db.engine.dispose


@rung("L3c", "ORM session, column select(Item.id, .name, .value) -> Row (no instance)")
async def _l3c():
    from sqlalchemy import select

    db = _bare()
    Item = db.Item

    async def op(i: int):
        async with db.SessionLocal() as s:
            return (
                await s.execute(
                    select(Item.id, Item.name, Item.value).where(Item.id == _rid(i))
                )
            ).first()

    return op, db.engine.dispose


@rung("L3t", "L3 inside session.begin() (BEGIN/COMMIT, as Sandloom's UoW)")
async def _l3t():
    db = _bare()

    async def op(i: int):
        async with db.SessionLocal() as s, s.begin():
            return await s.get(db.Item, _rid(i))

    return op, db.engine.dispose


@rung("L3g", "L3t + Sandloom's tenant guard installed (do_orm_execute + Session.get patch)")
async def _l3g():
    import sandloom.core.tenant  # noqa: F401  - installs the guard at import

    db = _bare()

    async def op(i: int):
        async with db.SessionLocal() as s, s.begin():
            return await s.get(db.Item, _rid(i))

    return op, db.engine.dispose


def _item_out():
    from pydantic import BaseModel, ConfigDict

    class ItemOut(BaseModel):
        model_config = ConfigDict(from_attributes=True)
        id: int
        name: str
        value: int

    return ItemOut


@rung("L4", "L3 + ItemOut(id=..,name=..,value=..).model_dump_json() (the S3 handler)")
async def _l4():
    db = _bare()
    ItemOut = _item_out()

    async def op(i: int):
        async with db.SessionLocal() as s:
            item = await s.get(db.Item, _rid(i))
        return ItemOut(id=item.id, name=item.name, value=item.value).model_dump_json()

    return op, db.engine.dispose


@rung("L4t", "L3t + ItemOut(...).model_dump_json() (BEGIN/COMMIT chain)")
async def _l4t():
    db = _bare()
    ItemOut = _item_out()

    async def op(i: int):
        async with db.SessionLocal() as s, s.begin():
            item = await s.get(db.Item, _rid(i))
        return ItemOut(id=item.id, name=item.name, value=item.value).model_dump_json()

    return op, db.engine.dispose


@rung("L4v", "L3 + ItemOut.model_validate(item, from_attributes) + model_dump_json")
async def _l4v():
    db = _bare()
    ItemOut = _item_out()

    async def op(i: int):
        async with db.SessionLocal() as s:
            item = await s.get(db.Item, _rid(i))
        return ItemOut.model_validate(item).model_dump_json()

    return op, db.engine.dispose


@rung("L4c", "L3 + ItemOut.model_construct(...) + model_dump_json")
async def _l4c():
    db = _bare()
    ItemOut = _item_out()

    async def op(i: int):
        async with db.SessionLocal() as s:
            item = await s.get(db.Item, _rid(i))
        return ItemOut.model_construct(
            id=item.id, name=item.name, value=item.value
        ).model_dump_json()

    return op, db.engine.dispose


@rung("L4ta", "L3 + TypeAdapter(ItemOut).dump_json(ItemOut(...))")
async def _l4ta():
    from pydantic import TypeAdapter

    db = _bare()
    ItemOut = _item_out()
    ta = TypeAdapter(ItemOut)

    async def op(i: int):
        async with db.SessionLocal() as s:
            item = await s.get(db.Item, _rid(i))
        return ta.dump_json(ItemOut(id=item.id, name=item.name, value=item.value))

    return op, db.engine.dispose


@rung("L4o", "L3c column Row -> orjson.dumps(row._asdict()) (no instance, no pydantic)")
async def _l4o():
    import orjson
    from sqlalchemy import select

    db = _bare()
    Item = db.Item

    async def op(i: int):
        async with db.SessionLocal() as s:
            row = (
                await s.execute(
                    select(Item.id, Item.name, Item.value).where(Item.id == _rid(i))
                )
            ).first()
        return orjson.dumps(row._asdict())

    return op, db.engine.dispose


# --- Sandloom ------------------------------------------------------------------- #
class _Lifespan:
    """Minimal ASGI lifespan driver (startup on enter, shutdown on exit)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._rx: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._tx: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> _Lifespan:
        async def receive():
            return await self._rx.get()

        async def send(msg):
            await self._tx.put(msg)

        self._task = asyncio.ensure_future(
            self.app(
                {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                receive,
                send,
            )
        )
        await self._rx.put({"type": "lifespan.startup"})
        msg = await self._tx.get()
        if msg["type"] != "lifespan.startup.complete":
            raise RuntimeError(f"lifespan startup failed: {msg}")
        return self

    async def aclose(self) -> None:
        await self._rx.put({"type": "lifespan.shutdown"})
        await self._tx.get()
        await self._task


async def _sandloom_app():
    from benchsuite.apps.sandloom_app import app

    life = await _Lifespan(app).__aenter__()
    return app, life


@rung(
    "L5e",
    "Sandloom engine (pool hooks, linger pool) + plain AsyncSession.begin + get + ItemOut",
)
async def _l5e():
    from sqlalchemy.ext.asyncio import AsyncSession

    from sandloom.core.runtime import get_active_core
    from benchsuite.bench.api import ItemOut
    from benchsuite.bench.models import BenchItem

    _app, life = await _sandloom_app()
    engine = get_active_core().database("default").engine

    async def op(i: int):
        async with AsyncSession(engine, expire_on_commit=False) as s, s.begin():
            item = await s.get(BenchItem, _rid(i))
        return ItemOut(id=item.id, name=item.name, value=item.value).model_dump_json()

    return op, life.aclose


@rung("KG0", "one subject-record GET as the live check does it (security cache)")
async def _kg0():
    _s5_env("uow")
    from sandloom.core.auth.marks import subject_key
    from sandloom.core.infra import access

    _app, life = await _sandloom_app()
    key = subject_key("00000000-0000-4000-8000-00000000c0de")

    async def op(_i: int):
        return await access.security_cache().get(key)

    return op, life.aclose


@rung("KG1", "one GET through the redis-py client (execute_command)")
async def _kg1():
    _s5_env("uow")
    from sandloom.core.drivers.redis_cache import get_default_client

    _app, life = await _sandloom_app()
    client = await get_default_client()

    async def op(_i: int):
        return await client.execute_command("GET", "sandloom:auth:subject:kg")

    return op, life.aclose


@rung("KG2", "one GET on a pooled redis-py connection (send + read, no client layer)")
async def _kg2():
    _s5_env("uow")
    from sandloom.core.drivers.redis_cache import get_default_client

    _app, life = await _sandloom_app()
    client = await get_default_client()
    pool = client.connection_pool

    async def op(_i: int):
        conn = await pool.get_connection("GET")
        try:
            await conn.send_packed_command(
                conn.pack_command("GET", "sandloom:auth:subject:kg"), check_health=False
            )
            return await conn.read_response()
        finally:
            await pool.release(conn)

    return op, life.aclose


@rung("L5", "Sandloom db_session() UoW (tenant scope, outbox hooks) + get + ItemOut")
async def _l5():
    from sandloom.core.di import db_session
    from benchsuite.bench.api import ItemOut
    from benchsuite.bench.models import BenchItem

    _app, life = await _sandloom_app()

    async def op(i: int):
        async with db_session() as s:
            item = await s.get(BenchItem, _rid(i))
        return ItemOut(id=item.id, name=item.name, value=item.value).model_dump_json()

    return op, life.aclose


def _asgi_op(
    app: Any,
    prefix: str,
    *,
    scope_app: Any = None,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    rid: Callable[[int], int] | None = None,
) -> Op:
    rid = rid or _rid
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"user-agent", b"ladder"),
            (b"accept", b"*/*"),
            *headers,
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    req = {"type": "http.request", "body": b"", "more_body": False}

    async def receive():
        return req

    async def op(i: int):
        path = f"{prefix}{rid(i)}"
        out: list[Any] = []

        async def send(msg):
            out.append(msg)

        scope = dict(base)
        scope["path"] = path
        scope["raw_path"] = path.encode()
        scope["state"] = {}
        if scope_app is not None:  # calling app.router directly
            scope["app"] = scope_app
        await app(scope, receive, send)
        if out[0]["status"] != 200:
            raise RuntimeError(f"{path}: {out}")
        return out[1]["body"]

    return op


@rung("L6", "Sandloom app, full ASGI stack in-process: GET /s3/{id}")
async def _l6():
    app, life = await _sandloom_app()
    return _asgi_op(app, "/s3/"), life.aclose


def _s5_env(variant: str) -> str:
    """S5 server env (owner DSN → the NON-superuser app role) + a tenant-A token.

    Must run before the first ``sandloom`` import (settings are read at import).
    """
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("the S5 env must be set before importing sandloom")
    from benchsuite import s5

    os.environ.update(s5.server_env(variant))
    return s5.mint()


def _s5_op(app: Any, prefix: str, token: str) -> Op:
    from benchsuite import common

    n = common.S5_ROWS_PER_TENANT  # ids 1..n belong to tenant A
    return _asgi_op(
        app,
        prefix,
        headers=((b"authorization", f"Bearer {token}".encode()),),
        rid=lambda i: (_rid(i) - 1) % n + 1,
    )


@rung("L6S5", "S5 secure read, full ASGI in-process: GET /s5/{id} (JWT+tenant+RLS)")
async def _l6s5():
    token = _s5_env("uow")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5/", token), life.aclose


@rung("L6S5p", "S5 planner, GENERAL executor in-process: GET /s5plan/{id}")
async def _l6s5p():
    token = _s5_env("plan")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5plan/", token), life.aclose


@rung("L6S5po", "S5 planner, OPTIMIZED executor in-process: GET /s5plan/{id}")
async def _l6s5po():
    token = _s5_env("planopt")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5plan/", token), life.aclose


@rung("L6S5two", "S5 with TWO reads on the request UoW: GET /s5two/{id}")
async def _l6s5two():
    token = _s5_env("uow2")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5two/", token), life.aclose


@rung("L6S5p2", "S5 TWO declared reads, GENERAL executor: GET /s5plan2/{id}")
async def _l6s5p2():
    token = _s5_env("plan2")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5plan2/", token), life.aclose


@rung("L6S5po2", "S5 TWO declared reads, OPTIMIZED executor: GET /s5plan2/{id}")
async def _l6s5po2():
    token = _s5_env("planopt2")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5plan2/", token), life.aclose


def _diag_rung(stage: str):
    async def factory():
        from benchsuite import common

        token = _s5_env(stage)
        app, life = await _sandloom_app()
        return _s5_op(app, common.S5_DIAG_STAGES[stage][0], token), life.aclose

    return factory


#: S5 cost-attribution stages (DIAGNOSTIC ONLY — see benchsuite.bench.api.diag_s5).
for _n, _d in enumerate(
    (
        "S5-0 plain PK read, TenantScoped table, unscoped(), owner DSN, no RLS/auth",
        "S5-1 + JWT parse/verify (revocation lookup DISABLED, diag)",
        "S5-2 + TenantScope binding + ORM guard filter",
        "S5-3 + PostgreSQL RLS (tenant_rls hook, NOBYPASSRLS app role)",
        "S5-4 + Redis revocation check",
        "S5-5 + require(Subscribed(of=Account.user()))",
        "S5-6 the real /s5/ route (control: == S5-5)",
    )
):
    rung(f"D{_n}", _d)(_diag_rung(f"d{_n}"))


@rung("S5V", "S5 variant from LADDER_S5_VARIANT (uow|ro|fast|fn|plan|planopt|…)")
async def _s5v():
    from benchsuite import common

    variant = os.environ["LADDER_S5_VARIANT"]
    token = _s5_env(variant)
    app, life = await _sandloom_app()
    return _s5_op(app, common.S5_ROUTES[variant], token), life.aclose


@rung("L6S5f", "S5 fn variant in-process: GET /s5fn/{id} (1 RT, plpgsql)")
async def _l6s5f():
    token = _s5_env("fn")
    app, life = await _sandloom_app()
    return _s5_op(app, "/s5fn/", token), life.aclose


def _no_membership_contract() -> None:
    """DIAGNOSTIC: billing's tenant resolution without the tenancy contract call.

    ``resolve_tenant`` answers the seeded organization directly, so the rung
    minus this variant = what the ``tenancy.membership.v1`` read costs.
    """
    from sandloom_billing.api import billing as billing_api
    from sandloom_billing.services.tenant_resolver import (
        TENANT_ORGANIZATION,
        TenantContext,
    )
    from benchsuite import platform

    async def resolve_tenant(*, user_id: Any, **_kw: Any) -> TenantContext:
        return TenantContext(TENANT_ORGANIZATION, platform.ORG, user_id)

    billing_api.resolve_tenant = resolve_tenant  # type: ignore[assignment]


def _platform_rung(key: str, *, patch: Callable[[], None] | None = None):
    """A platform GET of ``sandloom.main:app`` (``benchsuite.platform``; seed first)."""

    async def factory():
        if "sandloom.core.config.settings" in sys.modules:
            raise RuntimeError("the platform env must be set before importing sandloom")
        from benchsuite import platform

        os.environ.update(platform.server_env())
        os.environ.pop("MODULE_REQUIRED", None)
        token = platform.mint()
        from sandloom.main import app

        if patch is not None:
            patch()

        life = await _Lifespan(app).__aenter__()
        op = _asgi_op(
            app,
            platform.ENDPOINTS[key][1],
            headers=((b"authorization", f"Bearer {token}".encode()),),
            rid=lambda _i: "",
        )
        return op, life.aclose

    return factory


#: Platform endpoints (identity / tenancy / billing GETs, ``benchsuite.platform``).
for _key in ("Pme", "Porg", "Porgs", "Pmem", "Psub", "Pcred"):
    rung(_key, f"platform app (sandloom.main), full ASGI in-process: {_key}")(
        _platform_rung(_key)
    )
for _key in ("Psub", "Pcred"):
    rung(f"{_key}NC", f"{_key} WITHOUT the membership contract call (DIAGNOSTIC)")(
        _platform_rung(_key, patch=_no_membership_contract)
    )


def _contract_rung(stage: str):
    """DIAGNOSTIC: the ``tenancy.membership.v1`` read billing makes, in pieces.

    ``C0`` opens + closes the provider's contract Unit of Work (no statement);
    ``C1`` runs the provider's query in it (no contract machinery); ``C2`` is
    the real local contract call inside a request read scope.
    """

    async def factory():
        if "sandloom.core.config.settings" in sys.modules:
            raise RuntimeError("the platform env must be set before importing sandloom")
        from benchsuite import platform

        os.environ.update(platform.server_env())
        os.environ.pop("MODULE_REQUIRED", None)
        from sandloom.core.module_api import contract_read_scope
        from sandloom.core.runtime import get_active_core
        from sandloom.main import app
        from sandloom_tenancy.services.membership_service import MembershipService

        life = await _Lifespan(app).__aenter__()
        core = get_active_core()
        factory_ = core.get_contract_uow_factory("default")
        svc = core.get_service("tenancy.membership.v1")
        user = platform.USER

        async def c0(_i: int) -> Any:
            async with factory_() as unit:
                await unit.session.connection()

        async def c1(_i: int) -> Any:
            async with factory_() as unit:
                return await MembershipService.list_user_organization_ids(
                    user, unit.session
                )

        async def c2(_i: int) -> Any:
            async with contract_read_scope():
                return await svc.list_user_organization_ids(user)

        return {"C0": c0, "C1": c1, "C2": c2}[stage], life.aclose

    return factory


for _stage, _d in (
    ("C0", "contract UoW open/close only (DIAGNOSTIC)"),
    ("C1", "C0 + the membership query, no contract machinery (DIAGNOSTIC)"),
    ("C2", "the real local tenancy.membership.v1 read in a read scope (DIAGNOSTIC)"),
):
    rung(_stage, _d)(_contract_rung(_stage))


def _bare_router(app: Any) -> Any:
    """``app.router`` plus FastAPI's own AsyncExitStackMiddleware (which the
    endpoint machinery requires); no Starlette error/exception middleware and
    no user middleware."""
    from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware

    return AsyncExitStackMiddleware(app.router)


@rung(
    "L6R",
    "S3 handler verbatim, db DECLARED ReadOnlyDbSession, DB_READ_ONLY_SESSIONS=true",
)
async def _l6ro():
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("DB_READ_ONLY_SESSIONS must be set before importing sandloom")
    os.environ["DB_READ_ONLY_SESSIONS"] = "true"
    app, life = await _fast_app()
    return _asgi_op(app, "/ro/"), life.aclose


@rung("L6R0", "same ReadOnlyDbSession route with the setting OFF (== DbSession)")
async def _l6ro0():
    app, life = await _fast_app()
    return _asgi_op(app, "/ro/"), life.aclose


@rung(
    "L6r",
    "Sandloom app.router only: routing + DbSession UoW + FastAPI serialization, NO middleware",
)
async def _l6r():
    app, life = await _sandloom_app()
    op = _asgi_op(_bare_router(app), "/s3/", scope_app=app)
    return op, life.aclose


def _l6_without(cls_name: str):
    async def factory():
        from benchsuite.apps.sandloom_app import app

        before = len(app.user_middleware)
        app.user_middleware = [
            m for m in app.user_middleware if m.cls.__name__ != cls_name
        ]
        if len(app.user_middleware) != before - 1:
            raise SystemExit(f"no middleware {cls_name!r} on the Sandloom app")
        life = await _Lifespan(app).__aenter__()
        return _asgi_op(app, "/s3/"), life.aclose

    return factory


#: ``L6-<MiddlewareClass>``: the Sandloom app with that ONE middleware removed
#: (L6 minus it = what the middleware costs on this request).
SANDLOOM_MIDDLEWARES = (
    "RpcFastPathMiddleware",
    "ContractReadScopeMiddleware",
    "CORSMiddleware",
    "Mask5xxMiddleware",
    "RequestIDMiddleware",
    "SecurityHeadersMiddleware",
    "IdempotencyCaptureMiddleware",
    "ProcessTimeMiddleware",
    "BodySizeLimitMiddleware",
    "RouteErrorMiddleware",
)
for _mw in SANDLOOM_MIDDLEWARES:
    RUNGS[f"L6-{_mw}"] = (f"Sandloom app without {_mw}", _l6_without(_mw))


@rung("L6fr", "bare FastAPI app.router only (no Starlette error/exception middleware)")
async def _l6fr():
    _bare()  # fixed pool BEFORE the app imports engine / SessionLocal
    from benchsuite.apps.fastapi_app import app

    life = await _Lifespan(app).__aenter__()
    return _asgi_op(_bare_router(app), "/s3/", scope_app=app), life.aclose


@rung("L6fa", "bare FastAPI app (benchsuite fastapi_app) in-process: GET /s3/{id}")
async def _l6fa():
    _bare()  # fixed pool BEFORE the app imports engine / SessionLocal
    from benchsuite.apps.fastapi_app import app

    life = await _Lifespan(app).__aenter__()
    return _asgi_op(app, "/s3/"), life.aclose


class _FastLife:
    def __init__(self, life: _Lifespan) -> None:
        self.life = life

    async def aclose(self) -> None:
        import fastpath

        await fastpath.dispose()
        await self.life.aclose()


async def _fast_app():
    import fastpath

    life = await _Lifespan(fastpath.app).__aenter__()
    return fastpath.app, _FastLife(life)


@rung("L7u", "Sandloom app + fast path F1: DbSession UoW, column select -> orjson Response")
async def _l7u():
    app, life = await _fast_app()
    return _asgi_op(app, "/fast/uow/"), life.aclose


@rung("L7a", "Sandloom app + fast path F2: Core AUTOCOMMIT conn, column select -> orjson")
async def _l7a():
    app, life = await _fast_app()
    return _asgi_op(app, "/fast/core/"), life.aclose


@rung("L7A", "Sandloom app + fast path F2b: Core on a dedicated AUTOCOMMIT engine -> orjson")
async def _l7A():
    app, life = await _fast_app()
    return _asgi_op(app, "/fast/core2/"), life.aclose


@rung("L7p", "Sandloom app + fast path F3: raw asyncpg pool fetchrow -> orjson")
async def _l7p():
    app, life = await _fast_app()
    return _asgi_op(app, "/fast/pg/"), life.aclose


# --------------------------------------------------------------------------- #
# Measurement modes
# --------------------------------------------------------------------------- #
# The two DIAGNOSTIC stands (RPC cost decomposition R*, write path L6W*).
import stands  # noqa: E402

stands.install(sys.modules[__name__])
# MIX: one request of the SaaS mix catalog, mono or one split service.
import mixrung  # noqa: E402

mixrung.install(sys.modules[__name__])
# H*: the hot-path profiling stand (hot_paths.py).
import hot_paths  # noqa: E402

hot_paths.install(sys.modules[__name__])
# WL*: the write / login profiling stand (write_login.py).
import write_login  # noqa: E402

write_login.install(sys.modules[__name__])


async def _run_batch(op: Op, start: int, n: int, conc: int) -> None:
    if conc <= 1:
        for i in range(start, start + n):
            await op(i)
        return
    per = n // conc

    async def worker(k: int) -> None:
        base = start + k * per
        for i in range(base, base + per):
            await op(i)

    await asyncio.gather(*(worker(k) for k in range(conc)))


async def mode_cpu(op: Op, a: argparse.Namespace) -> dict[str, Any]:
    await _run_batch(op, 0, a.warmup, a.conc)
    gen0: list[int] = [0]

    def cb(phase: str, info: dict[str, Any]) -> None:
        if phase == "start" and info["generation"] == 0:
            gen0[0] += 1

    gc.callbacks.append(cb)
    connects = [0]
    try:
        from sqlalchemy import event as _ev
        from sqlalchemy.pool import Pool

        def _on_connect(*_a: Any) -> None:
            connects[0] += 1

        _ev.listen(Pool, "connect", _on_connect)
    except ImportError:  # pragma: no cover
        pass
    cpu, wall, g0, usr, sy = [], [], [], [], []
    for rep in range(a.reps):
        gen0[0] = 0
        r0 = resource.getrusage(resource.RUSAGE_SELF)
        c0, w0 = time.process_time(), time.perf_counter()
        await _run_batch(op, (rep + 1) * a.n, a.n, a.conc)
        c1, w1 = time.process_time(), time.perf_counter()
        r1 = resource.getrusage(resource.RUSAGE_SELF)
        usr.append((r1.ru_utime - r0.ru_utime) / a.n * 1e6)
        sy.append((r1.ru_stime - r0.ru_stime) / a.n * 1e6)
        cpu.append((c1 - c0) / a.n * 1e6)
        wall.append((w1 - w0) / a.n * 1e6)
        g0.append(gen0[0] * 1000 / a.n)
    gc.callbacks.remove(cb)
    return {
        "cpu_us": statistics.median(cpu),
        "cpu_us_min": min(cpu),
        "cpu_us_max": max(cpu),
        "wall_us": statistics.median(wall),
        "wall_us_min": min(wall),
        "wall_us_max": max(wall),
        "wait_us": statistics.median(w - c for w, c in zip(wall, cpu, strict=True)),
        "gen0_per_1k": statistics.median(g0),
        "user_us": statistics.median(usr),
        "sa_new_connections": connects[0],
        "sys_us": statistics.median(sy),
        "cpu_all": [round(x, 1) for x in cpu],
    }


def _site(filename: str, lineno: int) -> str:
    fn = str(filename)
    for marker in ("site-packages/", "/src/", "/benchmarks/", "/lib/python3"):
        if marker in fn:
            fn = fn.split(marker, 1)[1]
            break
    return f"{fn}:{lineno}"


async def mode_alloc(op: Op, a: argparse.Namespace) -> dict[str, Any]:
    import tracemalloc

    await _run_batch(op, 0, a.warmup, a.conc)
    gc.collect()
    gc.disable()
    try:
        # (1) net pymalloc blocks + gen0 counter growth with the collector off:
        # everything still alive after the batch is cyclic garbage (or a cache).
        gc.collect()
        b0, n0 = sys.getallocatedblocks(), gc.get_count()[0]
        await _run_batch(op, a.warmup, a.n, a.conc)
        b1, n1 = sys.getallocatedblocks(), gc.get_count()[0]
        unreachable = gc.collect()
        b2 = sys.getallocatedblocks()
        # (2) tracemalloc: where that retained (cyclic) garbage is allocated.
        tracemalloc.start(1)
        s0 = tracemalloc.take_snapshot()
        await _run_batch(op, a.warmup + a.n, a.n, a.conc)
        s1 = tracemalloc.take_snapshot()
        tracemalloc.stop()
        flt = [tracemalloc.Filter(False, tracemalloc.__file__)]
        diff = s1.filter_traces(flt).compare_to(s0.filter_traces(flt), "lineno")
        top = sorted(diff, key=lambda d: -d.count_diff)[:15]
        gc.collect()
    finally:
        gc.enable()
    n = a.n
    return {
        "blocks_retained_gc_off_per_op": (b1 - b0) / n,
        "gen0_count_growth_per_op": (n1 - n0) / n,
        "cyclic_garbage_objects_per_op": unreachable / n,
        "blocks_after_collect_per_op": (b2 - b0) / n,
        "tracemalloc_retained_blocks_per_op": sum(d.count_diff for d in diff) / n,
        "tracemalloc_top": [
            [
                _site(d.traceback[0].filename, d.traceback[0].lineno),
                round(d.count_diff / n, 2),
                round(d.size_diff / n),
            ]
            for d in top
        ],
    }


_LAYERS = (
    ("asyncpg", "asyncpg"),
    ("sqlalchemy/orm", "sqlalchemy.orm"),
    ("sqlalchemy/ext/asyncio", "sqlalchemy.asyncio"),
    ("sqlalchemy/dialects", "sqlalchemy.dialect"),
    ("sqlalchemy/engine", "sqlalchemy.engine"),
    ("sqlalchemy/pool", "sqlalchemy.pool"),
    ("sqlalchemy", "sqlalchemy.other"),
    ("greenlet", "greenlet"),
    ("pydantic", "pydantic"),
    ("fastapi", "fastapi"),
    ("starlette", "starlette"),
    ("anyio", "anyio"),
    ("/sandloom/core/http", "sandloom.http"),
    ("/sandloom/core/observability", "sandloom.observability"),
    ("/sandloom/core/unit_of_work", "sandloom.uow"),
    ("/sandloom/core/tenant", "sandloom.tenant"),
    ("/sandloom/", "sandloom.other"),
    ("benchsuite", "app"),
    ("ladder", "ladder-harness"),
    ("asyncio", "asyncio"),
    ("contextlib", "contextlib"),
)


def _layer(filename: str) -> str:
    for needle, name in _LAYERS:
        if needle in filename:
            return name
    return "other:" + filename.rsplit("/", 1)[-1]


async def mode_memray(op: Op, a: argparse.Namespace) -> dict[str, Any]:
    import memray

    await _run_batch(op, 0, a.warmup, a.conc)
    path = Path(a.tmp) / f"memray-{a.rung}-{os.getpid()}.bin"
    path.unlink(missing_ok=True)
    gc.collect()
    with memray.Tracker(
        path, trace_python_allocators=True, native_traces=False, follow_fork=False
    ):
        await _run_batch(op, a.warmup, a.n, a.conc)
    reader = memray.FileReader(path)
    sites: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    total = 0
    for rec in reader.get_allocation_records():
        kind = memray.AllocatorType(rec.allocator).name
        if "FREE" in kind or "MUNMAP" in kind:
            continue
        cnt = rec.n_allocations
        total += cnt
        st = rec.stack_trace(max_stacks=1)
        if st:
            fn, file, line = st[0]
            sites[f"{_site(file, line)} ({fn})"] += cnt
            layers[_layer(file)] += cnt
        else:
            sites["<no python frame>"] += cnt
            layers["<native>"] += cnt
    path.unlink(missing_ok=True)
    n = a.n
    return {
        "allocs_per_op": total / n,
        "layers": {k: round(v / n, 2) for k, v in layers.most_common()},
        "top": [[k, round(v / n, 2)] for k, v in sites.most_common(15)],
    }


async def mode_prof(op: Op, a: argparse.Namespace) -> dict[str, Any]:
    """cProfile per-function own time (tottime, µs/op) and calls/op, top 80."""
    import cProfile
    import pstats

    await _run_batch(op, 0, a.warmup, a.conc)
    prof = cProfile.Profile()
    prof.enable()
    await _run_batch(op, a.warmup, a.n, a.conc)
    prof.disable()
    st = pstats.Stats(prof)
    rows = []
    for (file, line, fn), (_cc, nc, tt, ct, _callers) in st.stats.items():
        rows.append(
            [f"{_site(file, line)}({fn})", nc / a.n, tt / a.n * 1e6, ct / a.n * 1e6]
        )
    rows.sort(key=lambda r: -r[2])
    total = sum(r[2] for r in rows)
    return {
        "prof_total_us": total,
        "funcs": [
            [r[0], round(r[1], 2), round(r[2], 2), round(r[3], 2)] for r in rows[:120]
        ],
    }


async def mode_calls(op: Op, a: argparse.Namespace) -> dict[str, Any]:
    import cProfile
    import pstats

    await _run_batch(op, 0, a.warmup, a.conc)
    prof = cProfile.Profile()
    prof.enable()
    await _run_batch(op, a.warmup, a.n, a.conc)
    prof.disable()
    st = pstats.Stats(prof)
    total = sum(v[1] for v in st.stats.values())  # ncalls incl. recursion
    layers: Counter[str] = Counter()
    for (file, _line, _fn), v in st.stats.items():
        layers[_layer(file) if file != "~" else "builtins"] += v[1]
    top = sorted(st.stats.items(), key=lambda kv: -kv[1][1])[:15]
    return {
        "calls_per_op": total / a.n,
        "layers": {k: round(v / a.n, 1) for k, v in layers.most_common()},
        "top": [
            [f"{k[0].rsplit('/', 1)[-1]}:{k[1]}({k[2]})", round(v[1] / a.n, 2)]
            for k, v in top
        ],
    }


def _load_pgcount():
    path = HERE.parent.parent / "tests/perf_budgets/pgcount.py"
    spec = importlib.util.spec_from_file_location("ladder_pgcount", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ladder_pgcount"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


async def mode_rt(a: argparse.Namespace, factory) -> dict[str, Any]:
    from urllib.parse import urlsplit, urlunsplit

    pgcount = _load_pgcount()
    url = os.environ["DATABASE_URL"]
    parts = urlsplit(url)
    proxy = pgcount.PgCountingProxy(parts.hostname, parts.port or 5432)
    await proxy.start()
    netloc = parts.netloc.rsplit("@", 1)
    netloc[-1] = f"127.0.0.1:{proxy.port}"
    os.environ["DATABASE_URL"] = urlunsplit(parts._replace(netloc="@".join(netloc)))
    # Redis commands: count at the client (every command / pipeline).
    import redis.asyncio.client as _rc

    redis_sent: list[str] = []
    _orig_exec = _rc.Redis.execute_command
    _orig_pipe = _rc.Pipeline.execute

    async def _counting(self: Any, *args: Any, **kw: Any) -> Any:
        redis_sent.append(str(args[0]).upper())
        return await _orig_exec(self, *args, **kw)

    async def _counting_pipe(self: Any, *args: Any, **kw: Any) -> Any:
        redis_sent.append(f"PIPELINE[{len(self.command_stack)}]")
        return await _orig_pipe(self, *args, **kw)

    _rc.Redis.execute_command = _counting  # type: ignore[method-assign]
    _rc.Pipeline.execute = _counting_pipe  # type: ignore[method-assign]
    op, teardown = await factory()
    await _run_batch(op, 0, a.warmup, 1)
    proxy.reset()
    redis_sent.clear()
    await op(a.warmup)
    one = proxy.reset()
    redis_one = list(redis_sent)
    redis_sent.clear()
    await _run_batch(op, a.warmup + 1, a.n, 1)
    total = proxy.count
    redis_total = len(redis_sent)
    await teardown()
    try:
        await asyncio.wait_for(proxy.stop(), 10)
    except TimeoutError:
        pass
    return {
        "rt_per_op": total / a.n,
        "one_op": [rt.label() for rt in one],
        "redis_per_op": redis_total / a.n,
        "redis_one_op": redis_one,
        "new_connections": proxy.connections,
    }


def _new_loop_factory(kind: str):
    if kind == "uvloop":
        import uvloop

        return uvloop.new_event_loop
    return asyncio.new_event_loop


async def _main(a: argparse.Namespace) -> dict[str, Any]:
    desc, factory = RUNGS[a.rung]
    if a.mode == "rt":
        res = await mode_rt(a, factory)
    else:
        op, teardown = await factory()
        try:
            if a.mode == "cg":
                await _run_batch(op, 0, 20, 1)
                # LADDER_CG_GC (diagnostic): "freeze" = gc.collect() + gc.freeze()
                # after the warm-up (production's GC_FREEZE_AFTER_STARTUP: the
                # startup heap is never traversed again), "off" = no collector
                # during the measured ops. Default: the collector as configured.
                gc_mode = os.environ.get("LADDER_CG_GC", "")
                if gc_mode in ("freeze", "off"):
                    gc.collect()
                    gc.freeze()
                if gc_mode == "off":
                    gc.disable()
                await _run_batch(op, 20, a.n, 1)
                res = {}
            else:
                res = await {
                    "cpu": mode_cpu,
                    "alloc": mode_alloc,
                    "memray": mode_memray,
                    "calls": mode_calls,
                    "prof": mode_prof,
                    "layers": write_login.mode_layers,
                }[a.mode](op, a)
        finally:
            await teardown()
    return {
        "rung": a.rung,
        "desc": desc,
        "mode": a.mode,
        "n": a.n,
        "conc": a.conc,
        "loop": a.loop,
        **res,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "mode",
        choices=[
            "list",
            "cpu",
            "alloc",
            "memray",
            "calls",
            "rt",
            "cg",
            "prof",
            "layers",
        ],
    )
    p.add_argument("rung", nargs="?")
    p.add_argument("--n", type=int, default=5000)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--warmup", type=int, default=500)
    p.add_argument("--conc", type=int, default=1)
    p.add_argument("--loop", default="asyncio", choices=["asyncio", "uvloop"])
    p.add_argument("--tmp", default=os.environ.get("TMPDIR", "/tmp"))
    a = p.parse_args()
    if a.mode == "list":
        for k, (d, _) in RUNGS.items():
            print(f"{k:6s} {d}")
        return
    with asyncio.Runner(loop_factory=_new_loop_factory(a.loop)) as runner:
        res = runner.run(_main(a))
    if a.mode != "cg":
        print(json.dumps(res))


if __name__ == "__main__":
    main()
