"""``DB_RLS_MERGED_BEGIN`` (default on): ``BEGIN; SELECT set_config(tenant)`` in ONE
simple-query message (real PostgreSQL, NOBYPASSRLS role, pool of ONE so any
state left on the pooled connection shows up in the next request).

After every failure / cancellation the pooled connection must carry no open
transaction and no tenant setting, and the next request (another tenant) must
see only its own rows.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextvars import copy_context
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.sql import column, table

from sandloom.core.di import bind_tenant
from sandloom.core.sql_alchemy import tenant_rls
from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.sql_alchemy.tenant_rls import install_tenant_rls
from sandloom.core.tenant_context import _current_tenant

from .pgcount import PgCountingProxy

pytestmark = pytest.mark.asyncio

A = uuid.UUID("00000000-0000-0000-0000-00000000a00a")
B = uuid.UUID("00000000-0000-0000-0000-00000000b00b")


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


class Env:
    def __init__(self, name: str, role: str, url: str, admin: AsyncEngine) -> None:
        self.name, self.role, self.url, self.admin = name, role, url, admin
        self.t = table(name, column("name"))
        self.slow = f"{name}_slow"

    def engine(self, *, merged: bool = True, **kw: Any) -> AsyncEngine:
        engine = create_async_engine(self.url, pool_size=1, max_overflow=0, **kw)
        install_tenant_rls(engine, merged_begin=merged)
        return engine

    async def names(self, engine: AsyncEngine) -> list[str]:
        async with AsyncSession(engine) as s, s.begin():
            rows = await s.execute(select(self.t.c.name).order_by(self.t.c.name))
            return [r[0] for r in rows]

    async def assert_clean(self, engine: AsyncEngine) -> int:
        """No open transaction, no tenant setting on the pooled connection
        (read on the raw driver connection — the hook does not run there).
        Returns the backend pid."""
        async with engine.connect() as conn:
            raw: Any = (await conn.get_raw_connection()).driver_connection
            assert not raw.is_in_transaction()
            assert await raw.fetchval(
                "SELECT current_setting('sandloom.tenant_id', true)"
            ) in ("", None)
            return int(await raw.fetchval("SELECT pg_backend_pid()"))

    async def assert_no_idle_in_transaction(self) -> None:
        async with self.admin.connect() as conn:
            for _ in range(20):
                n = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE usename = "
                            ":r AND state LIKE 'idle in transaction%'"
                        ),
                        {"r": self.role},
                    )
                ).scalar_one()
                if n == 0:
                    return
                await asyncio.sleep(0.05)
        raise AssertionError("a backend of the app role is idle in transaction")


@pytest_asyncio.fixture
async def env(pg_proxy: PgCountingProxy) -> AsyncIterator[Env]:
    suffix = uuid.uuid4().hex[:10]
    name, role, password = (
        f"perf_mb_{suffix}",
        f"sandloom_mb_{suffix}",
        uuid.uuid4().hex,
    )
    admin = create_async_engine(_url())
    async with admin.begin() as conn:
        await conn.execute(
            text(
                f"CREATE TABLE {name} (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
                "name text NOT NULL)"
            )
        )
        for sql in enable_tenant_rls_statements(name):
            await conn.execute(text(sql))
        # A table whose COMMIT takes 0.5 s (deferred constraint trigger).
        await conn.execute(text(f"CREATE TABLE {name}_slow (id int)"))
        await conn.execute(
            text(
                f"CREATE FUNCTION {name}_sleep() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN PERFORM pg_sleep(0.5); RETURN NULL; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE CONSTRAINT TRIGGER {name}_trg AFTER INSERT ON {name}_slow "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
                f"{name}_sleep()"
            )
        )
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"
            )
        )
        await conn.execute(text(f"GRANT SELECT ON {name} TO {role}"))
        await conn.execute(text(f"GRANT INSERT ON {name}_slow TO {role}"))
        await conn.execute(
            text(
                f"INSERT INTO {name} VALUES (gen_random_uuid(), '{A}', 'a1'), "
                f"(gen_random_uuid(), '{A}', 'a2'), (gen_random_uuid(), '{B}', 'b1')"
            )
        )
    app_url = (
        make_url(_url())
        .set(username=role, password=password, host="127.0.0.1", port=pg_proxy.port)
        .render_as_string(hide_password=False)
    )
    try:
        yield Env(name, role, app_url, admin)
    finally:
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = :r"
                ),
                {"r": role},
            )
            await conn.execute(text(f"DROP TABLE IF EXISTS {name}, {name}_slow"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {name}_sleep()"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


def _labels(proxy: PgCountingProxy) -> list[str]:
    return [" ".join(rt.sql).upper()[:120] for rt in proxy.reset()]


# --------------------------------------------------------------------------- #
async def test_three_round_trips_and_isolation(
    env: Env, pg_proxy: PgCountingProxy
) -> None:
    engine = env.engine()
    try:
        with bind_tenant(A):
            await env.names(engine)  # warm-up (connect, dialect init)
        pg_proxy.reset()
        with bind_tenant(A):
            assert await env.names(engine) == ["a1", "a2"]
        trips = _labels(pg_proxy)
        assert len(trips) == 3, trips
        assert (
            trips[0].startswith("BEGIN")
            and "SET_CONFIG('SANDLOOM.TENANT_ID'" in trips[0]
        )
        assert "SELECT" in trips[1] and trips[2].startswith("COMMIT")
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
        assert await env.names(engine) == []  # no tenant: fail closed
        for i in range(20):  # pool of one, alternating tenants
            tenant = (A, B)[i % 2]
            with bind_tenant(tenant):
                assert await env.names(engine) == (
                    ["a1", "a2"] if tenant == A else ["b1"]
                )
            await env.assert_clean(engine)
    finally:
        await engine.dispose()


async def test_default_on_with_a_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import Settings

    monkeypatch.delenv("DB_RLS_MERGED_BEGIN", raising=False)
    assert Settings(_env_file=None).db_rls_merged_begin is True  # type: ignore[call-arg]
    monkeypatch.setenv("DB_RLS_MERGED_BEGIN", "false")
    assert Settings(_env_file=None).db_rls_merged_begin is False  # type: ignore[call-arg]


async def test_kill_switch_keeps_four_round_trips(
    env: Env, pg_proxy: PgCountingProxy
) -> None:
    engine = env.engine(merged=False)
    try:
        with bind_tenant(A):
            await env.names(engine)
            pg_proxy.reset()
            assert await env.names(engine) == ["a1", "a2"]
        trips = _labels(pg_proxy)
        assert len(trips) == 4 and trips[0].startswith("BEGIN"), trips
    finally:
        await engine.dispose()


async def test_probe_fails_falls_back_to_the_plain_path(
    env: Env, pg_proxy: PgCountingProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed asyncpg / SQLAlchemy internals (probe reports them missing):
    the merged BEGIN is NOT used even though it is enabled — a separate BEGIN
    + set_config (4 round trips) — and isolation is unchanged."""
    from sandloom.core.sql_alchemy import driver_compat

    monkeypatch.setitem(
        driver_compat.PROBES,
        driver_compat.ASYNCPG_TRANSACTION,
        lambda: ["simulated: AsyncAdapt_asyncpg_connection._execute_mutex missing"],
    )
    driver_compat.reset_driver_probes()
    engine = env.engine(merged=True)
    try:
        with bind_tenant(A):
            await env.names(engine)
            pg_proxy.reset()
            assert await env.names(engine) == ["a1", "a2"]
        trips = _labels(pg_proxy)
        assert len(trips) == 4, trips
        assert trips[0].startswith("BEGIN") and "SET_CONFIG" not in trips[0]
        assert "SET_CONFIG('SANDLOOM.TENANT_ID'" in trips[1]
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
        assert await env.names(engine) == []
        await env.assert_clean(engine)
    finally:
        await engine.dispose()
        driver_compat.reset_driver_probes()


async def test_rollback_after_a_failed_statement(env: Env) -> None:
    engine = env.engine()
    try:
        with bind_tenant(A):
            with pytest.raises(DBAPIError, match="division by zero"):
                async with AsyncSession(engine) as s, s.begin():
                    await s.execute(text("SELECT 1/0"))
        await env.assert_clean(engine)
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
    finally:
        await engine.dispose()


async def test_savepoints(env: Env) -> None:
    engine = env.engine()
    try:
        with bind_tenant(A):
            async with AsyncSession(engine) as s, s.begin():
                q = select(env.t.c.name).order_by(env.t.c.name)
                assert list((await s.scalars(q)).all()) == ["a1", "a2"]
                nested = await s.begin_nested()
                assert list((await s.scalars(q)).all()) == ["a1", "a2"]
                with bind_tenant(B):
                    assert list((await s.scalars(q)).all()) == ["b1"]
                    await nested.rollback()  # server back on A, B still bound
                    assert list((await s.scalars(q)).all()) == ["b1"]
                assert list((await s.scalars(q)).all()) == ["a1", "a2"]
        await env.assert_clean(engine)
    finally:
        await engine.dispose()


async def test_invalidation_and_pre_ping(env: Env, pg_proxy: PgCountingProxy) -> None:
    engine = env.engine(pool_pre_ping=True)
    try:
        with bind_tenant(A):
            assert await env.names(engine) == ["a1", "a2"]
            async with engine.connect() as conn:
                await conn.invalidate()
            assert await env.names(engine) == ["a1", "a2"]  # fresh connection
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
        await env.assert_clean(engine)
    finally:
        await engine.dispose()


async def test_server_error_inside_the_merged_message(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = env.engine()
    try:
        pid = await env.assert_clean(engine)
        original = tenant_rls._merged_begin_sql
        monkeypatch.setattr(
            tenant_rls,
            "_merged_begin_sql",
            lambda adapter, value: original(adapter, value).replace(
                "set_config('sandloom.tenant_id', ",
                "set_config('sandloom.tenant_id', (1/0)::text || ",
            ),
        )
        with bind_tenant(A):
            with pytest.raises(DBAPIError, match="division by zero"):
                await env.names(engine)
        monkeypatch.setattr(tenant_rls, "_merged_begin_sql", original)
        assert await env.assert_clean(engine) != pid  # the connection was replaced
        await env.assert_no_idle_in_transaction()
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
    finally:
        await engine.dispose()


async def test_malformed_tenant_in_context_sends_nothing(env: Env) -> None:
    engine = env.engine()
    try:
        token = _current_tenant.set("not-a-uuid")  # type: ignore[arg-type]
        try:
            with pytest.raises((ValueError, DBAPIError)):
                await env.names(engine)
        finally:
            _current_tenant.reset(token)
        await env.assert_clean(engine)
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
    finally:
        await engine.dispose()


# --- cancellation at every await point ------------------------------------- #
async def _cancel_after(coro: Any, delay: float) -> None:
    task = asyncio.ensure_future(coro, loop=asyncio.get_running_loop())
    await asyncio.sleep(delay)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("point", ["merged", "between", "select", "commit"])
async def test_cancellation_leaves_nothing_on_the_pooled_connection(
    env: Env, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    engine = env.engine()
    try:
        pid = await env.assert_clean(engine)
        if point == "merged":  # the BEGIN; set_config reply is 1 s away
            original = tenant_rls._merged_begin_sql
            monkeypatch.setattr(
                tenant_rls,
                "_merged_begin_sql",
                lambda adapter, value: (
                    original(adapter, value) + "; SELECT pg_sleep(1)"
                ),
            )
        if point == "between":  # BEGIN + tenant done, SELECT not sent yet
            prepare: Any = AsyncAdapt_asyncpg_connection._prepare

            async def slow_prepare(self: Any, *a: Any, **kw: Any) -> Any:
                await asyncio.sleep(1)
                return await prepare(self, *a, **kw)

            monkeypatch.setattr(AsyncAdapt_asyncpg_connection, "_prepare", slow_prepare)

        async def request() -> None:
            with bind_tenant(A):
                async with AsyncSession(engine) as s, s.begin():
                    if point == "select":
                        await s.execute(
                            text(f"SELECT pg_sleep(1), name FROM {env.name}")
                        )
                    elif point == "commit":
                        await s.execute(text(f"INSERT INTO {env.slow} VALUES (1)"))
                    else:
                        await s.execute(select(env.t.c.name))

        await _cancel_after(copy_context().run(request), 0.3)
        monkeypatch.undo()
        await env.assert_no_idle_in_transaction()
        # a connection cancelled mid-transaction is never pooled again
        assert await env.assert_clean(engine) != pid
        with bind_tenant(B):
            assert await env.names(engine) == ["b1"]
        await env.assert_clean(engine)
    finally:
        await engine.dispose()
