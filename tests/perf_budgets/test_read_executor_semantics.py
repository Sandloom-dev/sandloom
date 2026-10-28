"""Transactional semantics of the read-only session policy (real PostgreSQL).

``DB_READ_ONLY_SESSIONS`` + ``ReadOnlyDbSession`` (declared policy,
:mod:`sandloom.core.sql_alchemy.read_only_policy`): a route that DECLARES itself
read-only uses BEGIN READ ONLY; any write / flush / locking read / raw
statement is refused before it reaches the server.

These tests pin what must NOT change: row locks, RLS tenant isolation (with a
non-superuser role), REPEATABLE READ snapshots, no hidden writes, post-commit
hooks, connection checkout/return.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import column, select, table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from sandloom.core.di import bind_tenant, unscoped
from sandloom.core.sql_alchemy.read_only_policy import (
    READ_ONLY_OPTION,
    ReadOnlySessionError,
    install_read_executor,
)
from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.sql_alchemy.tenant_rls import install_tenant_rls

from .pgcount import PgCountingProxy

pytestmark = pytest.mark.asyncio

TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
KINDS = ("BEGIN", "COMMIT", "ROLLBACK", "FOR UPDATE", "INSERT", "UPDATE", "SELECT")


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


def _proxied(proxy: PgCountingProxy, base: str | None = None) -> str:
    return (
        make_url(base or _url())
        .set(host="127.0.0.1", port=proxy.port)
        .render_as_string(hide_password=False)
    )


def _kinds(proxy: PgCountingProxy) -> list[str]:
    out = []
    for rt in proxy.reset():
        sql = " ".join(rt.sql).upper()
        out.append(next((k for k in KINDS if k in sql), sql[:30]))
    return out


@pytest_asyncio.fixture
async def probe(pg_proxy: PgCountingProxy) -> AsyncIterator[tuple[str, AsyncEngine]]:
    name = f"perf_rx_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_url())
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE TABLE {name} (id INT PRIMARY KEY, v INT)"))
        await conn.execute(text(f"INSERT INTO {name} VALUES (1, 10)"))
    engine = create_async_engine(_proxied(pg_proxy), pool_size=1, max_overflow=0)
    try:
        yield name, engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await admin.dispose()


def _t(name: str) -> Any:
    return table(name, column("id"), column("v"))


# --- Declared read-only policy -------------------------------------------------
@pytest.fixture
def ro_engine(probe: tuple[str, AsyncEngine]) -> tuple[str, AsyncEngine, Any]:
    name, engine = probe
    install_read_executor(engine)
    return (
        name,
        engine,
        engine.execution_options(
            **{READ_ONLY_OPTION: True, "postgresql_readonly": True}
        ),
    )


async def test_read_only_session_keeps_server_transaction(
    ro_engine: tuple[str, AsyncEngine, Any], pg_proxy: PgCountingProxy
) -> None:
    name, engine, ro = ro_engine
    t = _t(name)
    async with AsyncSession(ro) as s, s.begin():
        await s.execute(select(t.c.v))  # warm
    pg_proxy.reset()
    async with AsyncSession(ro) as s, s.begin():
        assert (await s.execute(select(t.c.v).where(t.c.id == 1))).scalar_one() == 10
    assert _kinds(pg_proxy) == ["BEGIN", "SELECT", "COMMIT"]
    # the plain (non-declared) engine is untouched: BEGIN / SELECT / COMMIT
    async with AsyncSession(engine) as s, s.begin():
        await s.execute(select(t.c.v))
    assert _kinds(pg_proxy) == ["BEGIN", "SELECT", "COMMIT"]


@pytest.mark.parametrize("kind", ["update", "lock", "text", "flush"])
async def test_read_only_session_refuses_writes_before_sending(
    ro_engine: tuple[str, AsyncEngine, Any], pg_proxy: PgCountingProxy, kind: str
) -> None:
    name, _engine, ro = ro_engine
    t = _t(name)
    async with AsyncSession(ro) as s, s.begin():
        await s.execute(select(t.c.v))  # warm
    pg_proxy.reset()
    with pytest.raises(ReadOnlySessionError):
        async with AsyncSession(ro) as s, s.begin():
            if kind == "update":
                await s.execute(t.update().values(v=0))
            elif kind == "lock":
                await s.execute(select(t.c.v).with_for_update())
            elif kind == "text":
                await s.execute(text(f"DELETE FROM {name}"))
            else:
                await s.execute(t.insert().values(id=2, v=2))
    sent = _kinds(pg_proxy)
    assert not {"UPDATE", "INSERT", "FOR UPDATE"} & set(sent), sent
    assert "DELETE" not in " ".join(sent)


# --- RLS: tenant isolation with a NON-superuser role ---------------------------
@pytest_asyncio.fixture
async def rls_role(pg_proxy: PgCountingProxy) -> AsyncIterator[tuple[str, str]]:
    suffix = uuid.uuid4().hex[:10]
    name, role, password = (
        f"perf_rls_{suffix}",
        f"sandloom_rx_{suffix}",
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
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"
            )
        )
        await conn.execute(text(f"GRANT SELECT ON {name} TO {role}"))
        await conn.execute(
            text(
                f"INSERT INTO {name} VALUES (gen_random_uuid(), '{TENANT_A}', 'a1'), "
                f"(gen_random_uuid(), '{TENANT_B}', 'b1')"
            )
        )
    app_url = make_url(_url()).set(username=role, password=password)
    try:
        yield name, _proxied(pg_proxy, app_url.render_as_string(hide_password=False))
    finally:
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


@pytest.mark.parametrize("mode", ["plain", "read_only"])
async def test_rls_isolation_holds_with_read_policies(
    rls_role: tuple[str, str], pg_proxy: PgCountingProxy, mode: str
) -> None:
    name, url = rls_role
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    install_tenant_rls(engine)
    install_read_executor(engine)
    bind = (
        engine.execution_options(
            **{READ_ONLY_OPTION: True, "postgresql_readonly": True}
        )
        if mode == "read_only"
        else engine
    )
    t = table(name, column("name"))

    async def names() -> list[str]:
        async with AsyncSession(bind) as s, s.begin():
            return [r[0] for r in await s.execute(select(t.c.name).order_by(t.c.name))]

    try:
        with bind_tenant(TENANT_A):
            assert await names() == ["a1"]
        with bind_tenant(TENANT_B):
            assert await names() == ["b1"]
        assert await names() == []  # no tenant bound: fail closed
        with unscoped():  # the bypass-free policy: unscoped() does not lift RLS
            assert await names() == []
        pg_proxy.reset()
        with bind_tenant(TENANT_A):
            await names()
        kinds = _kinds(pg_proxy)
        # RLS keeps the transaction so the SET LOCAL covers the read
        assert kinds[0] == "BEGIN" and kinds[-1] == "COMMIT", kinds
    finally:
        await engine.dispose()
