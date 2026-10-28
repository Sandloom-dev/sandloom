"""PostgreSQL Row-Level Security defense in depth (real PostgreSQL).

The ORM tenant guard cannot see raw ``text()`` / Core statements. With
``tenant_rls`` enabled on a database instance, the engine hook keeps the
transaction-local ``sandloom.tenant_id`` setting in sync with the tenant context,
and ``enable_tenant_rls`` installs a fail-closed policy with no bypass branch
(``unscoped()`` lifts only the ORM guard; see test_tenant_rls_bypass.py for the
system channel and the SQL-capable-attacker cases).

The test DB role (``test_user``) is a SUPERUSER, and superusers bypass RLS even
with FORCE — so the checks run through a dedicated NON-superuser, NOBYPASSRLS
login role created (and dropped) by the fixture. Production apps must likewise
connect as a role that is neither superuser nor BYPASSRLS.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Text, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import bind_tenant, unscoped
from sandloom.core.orm import TenantScoped, disable_tenant_rls, enable_tenant_rls
from sandloom.core.sql_alchemy.rls import (
    disable_tenant_rls_statements,
    enable_tenant_rls_statements,
)
from sandloom.core.sql_alchemy.tenant_rls import (
    TenantRLSConfigError,
    _set_config_sql,
    install_tenant_rls,
    is_tenant_rls_installed,
)
from sandloom.core.testing import reload_test_env

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _admin_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


class _Env:
    def __init__(self, table: str, app_engine: AsyncEngine, admin: AsyncEngine):
        self.table = table
        self.app_engine = app_engine
        self.admin = admin
        self.sessions = async_sessionmaker(app_engine, expire_on_commit=False)
        self.unhooked: AsyncEngine = app_engine  # replaced by the fixture

    async def names(self, session: Any) -> list[str]:
        rows = await session.execute(
            text(f"SELECT name FROM {self.table} ORDER BY name")
        )
        return [r[0] for r in rows]


@pytest_asyncio.fixture
async def rls() -> AsyncGenerator[_Env, None]:
    admin_url = _admin_url()
    suffix = uuid.uuid4().hex[:10]
    table = f"test_rls_notes_{suffix}"
    role = f"sandloom_rls_app_{suffix}"
    password = uuid.uuid4().hex
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(
                f"CREATE TABLE {table} (id uuid PRIMARY KEY, "
                "tenant_id uuid NOT NULL, name text NOT NULL)"
            )
        )
        for sql in enable_tenant_rls_statements(table):
            await conn.execute(text(sql))
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                "NOSUPERUSER NOBYPASSRLS"
            )
        )
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        )
        await conn.execute(
            text(
                f"INSERT INTO {table} VALUES "
                f"(gen_random_uuid(), '{TENANT_A}', 'a1'), "
                f"(gen_random_uuid(), '{TENANT_A}', 'a2'), "
                f"(gen_random_uuid(), '{TENANT_B}', 'b1')"
            )
        )
    app_url = make_url(admin_url).set(username=role, password=password)
    # pool_size=1 → every transaction reuses the SAME physical connection, so
    # stale per-connection state would show up.
    app_engine = create_async_engine(app_url, pool_size=1, max_overflow=0)
    install_tenant_rls(app_engine)
    env = _Env(table, app_engine, admin)
    env.unhooked = create_async_engine(app_url)
    try:
        yield env
    finally:
        await app_engine.dispose()
        await env.unhooked.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


@pytest.mark.asyncio
async def test_raw_text_sees_only_bound_tenant(rls: _Env) -> None:
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == ["a1", "a2"]
    with bind_tenant(TENANT_B):
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == ["b1"]


@pytest.mark.asyncio
async def test_no_tenant_and_not_unscoped_sees_nothing(rls: _Env) -> None:
    async with rls.sessions() as s, s.begin():
        assert await rls.names(s) == []
        # Core connection path too (no ORM involvement at all).
    async with rls.app_engine.connect() as conn:
        rows = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
        assert rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_unscoped_does_not_lift_rls(rls: _Env) -> None:
    # unscoped() is an ORM-guard escape only: the app role still sees nothing
    # without a tenant, and only the bound tenant with one.
    with unscoped():
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == []
    with bind_tenant(TENANT_A), unscoped():
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == ["a1", "a2"]


@pytest.mark.asyncio
async def test_settings_resync_within_one_transaction(rls: _Env) -> None:
    # Tenant bound AFTER the transaction began, then switched, then unscoped:
    # each statement sees the context current at execution time.
    async with rls.sessions() as s, s.begin():
        assert await rls.names(s) == []
        with bind_tenant(TENANT_A):
            assert await rls.names(s) == ["a1", "a2"]
            with bind_tenant(TENANT_B):
                assert await rls.names(s) == ["b1"]
            assert await rls.names(s) == ["a1", "a2"]
            with unscoped():
                assert await rls.names(s) == ["a1", "a2"]
        assert await rls.names(s) == []


@pytest.mark.asyncio
async def test_settings_do_not_leak_across_pooled_transactions(rls: _Env) -> None:
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            assert len(await rls.names(s)) == 2
    # Same physical connection (pool_size=1), new transaction, no context.
    async with rls.sessions() as s, s.begin():
        assert await rls.names(s) == []
    with bind_tenant(TENANT_B):
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == ["b1"]


@pytest.mark.asyncio
async def test_savepoint_rollback_forces_resync(rls: _Env) -> None:
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == ["a1", "a2"]
            with bind_tenant(TENANT_B):
                nested = await s.begin_nested()
                assert await rls.names(s) == ["b1"]
                # ROLLBACK TO SAVEPOINT reverts the SET LOCAL done inside it
                # (back to tenant A in PostgreSQL); the hook must re-sync.
                await nested.rollback()
                assert await rls.names(s) == ["b1"]
            assert await rls.names(s) == ["a1", "a2"]


@pytest.mark.asyncio
async def test_with_check_blocks_cross_tenant_writes(rls: _Env) -> None:
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s:
            with pytest.raises(DBAPIError, match="row-level security"):
                async with s.begin():
                    await s.execute(
                        text(
                            f"INSERT INTO {rls.table} VALUES "
                            f"(gen_random_uuid(), '{TENANT_B}', 'evil')"
                        )
                    )
        async with rls.sessions() as s, s.begin():
            # Raw UPDATE cannot touch another tenant's rows either.
            result = await s.execute(
                text(f"UPDATE {rls.table} SET name = 'x' WHERE name = 'b1'")
            )
            assert result.rowcount == 0  # type: ignore[attr-defined]
    # No tenant bound → writes are refused too (fail closed).
    async with rls.sessions() as s:
        with pytest.raises(DBAPIError, match="row-level security"):
            async with s.begin():
                await s.execute(
                    text(
                        f"INSERT INTO {rls.table} VALUES "
                        f"(gen_random_uuid(), '{TENANT_A}', 'nobody')"
                    )
                )


@pytest.mark.asyncio
async def test_policy_without_engine_hook_fails_closed(rls: _Env) -> None:
    # Proves the hook is what grants visibility: an engine WITHOUT tenant_rls
    # sees nothing even with a tenant bound / unscoped (settings never set).
    with bind_tenant(TENANT_A):
        async with rls.unhooked.connect() as conn:
            rows = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
            assert rows.scalar_one() == 0
    with unscoped():
        async with rls.unhooked.connect() as conn:
            rows = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
            assert rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_orm_tenant_scoped_model_works_with_rls(rls: _Env) -> None:
    class _B(DeclarativeBase):
        pass

    class _Note(TenantScoped, _B):
        __tablename__ = rls.table

        id: Mapped[uuid.UUID] = mapped_column(
            PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
        )
        name: Mapped[str] = mapped_column(Text)

    with bind_tenant(TENANT_B):
        async with rls.sessions() as s, s.begin():
            s.add(_Note(name="b2"))  # tenant inherited → passes WITH CHECK
        async with rls.sessions() as s, s.begin():
            names = (await s.execute(select(_Note.name).order_by(_Note.name))).all()
            assert [n for (n,) in names] == ["b1", "b2"]


@pytest.mark.asyncio
async def test_superuser_bypasses_rls_documented_caveat(rls: _Env) -> None:
    # Why the fixture needs a dedicated role: a superuser ignores policies even
    # with FORCE ROW LEVEL SECURITY.
    async with rls.admin.connect() as conn:
        rows = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
        assert rows.scalar_one() == 3


@pytest.mark.asyncio
async def test_disable_reverts_policy(rls: _Env) -> None:
    async with rls.admin.begin() as conn:
        for sql in disable_tenant_rls_statements(rls.table):
            await conn.execute(text(sql))
    async with rls.sessions() as s, s.begin():
        assert len(await rls.names(s)) == 3


def test_install_is_idempotent_and_postgres_only() -> None:
    engine = create_async_engine(_admin_url())
    install_tenant_rls(engine)
    install_tenant_rls(engine)
    assert is_tenant_rls_installed(engine)

    class _FakeDialect:
        name = "sqlite"

    class _FakeEngine:
        dialect = _FakeDialect()

    with pytest.raises(TenantRLSConfigError, match="PostgreSQL"):
        install_tenant_rls(_FakeEngine())  # type: ignore[arg-type]


def test_set_config_sql_is_parameterised() -> None:
    sql, params = _set_config_sql("numeric_dollar", str(TENANT_A))
    assert "$1" in sql and str(TENANT_A) not in sql
    assert params == (str(TENANT_A),)
    assert "tenant_bypass" not in sql
    sql, params = _set_config_sql("pyformat", str(TENANT_A))
    assert "%(t)s" in sql and params == {"t": str(TENANT_A)}


def test_migration_helper_emits_policy_through_op() -> None:
    class _Op:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def execute(self, sql: Any) -> None:
            self.sql.append(str(sql))

    op = _Op()
    enable_tenant_rls(op, "notes_note")
    joined = "\n".join(op.sql)
    assert 'ALTER TABLE "notes_note" ENABLE ROW LEVEL SECURITY' in joined
    assert 'ALTER TABLE "notes_note" FORCE ROW LEVEL SECURITY' in joined
    assert "current_setting('sandloom.tenant_id', true)" in joined
    # No bypass branch: nothing the app role can set opens other tenants.
    assert "tenant_bypass" not in joined
    assert " OR " not in joined
    # No system policy unless asked for (least privilege).
    assert "TO " not in joined.replace("NO FORCE", "")
    op_sys = _Op()
    enable_tenant_rls(op_sys, "notes_note", system_access=True)
    sys_sql = "\n".join(op_sys.sql)
    assert 'TO "sandloom_system" USING (true) WITH CHECK (true)' in sys_sql
    assert (
        'GRANT SELECT, INSERT, UPDATE, DELETE ON "notes_note" TO "sandloom_system"'
        in (sys_sql)
    )
    assert "WITH CHECK" in joined
    op2 = _Op()
    disable_tenant_rls(op2, "notes_note", schema="app")
    assert any('"app"."notes_note"' in s for s in op2.sql)
    with pytest.raises(ValueError, match="identifier"):
        enable_tenant_rls(op, 'x"; DROP TABLE users; --')


def test_driver_option_installs_hook() -> None:
    from sandloom.core.drivers.sqlalchemy_database import (
        SQLAlchemyDatabaseOptions,
        create_sqlalchemy_database,
    )
    from sandloom.core.infra.config import InstanceConfig

    assert SQLAlchemyDatabaseOptions(tenant_rls=True).tenant_rls is True
    provider = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="rls_probe",
            driver="sqlalchemy",
            options={"url": _admin_url(), "tenant_rls": True},
        )
    )
    assert is_tenant_rls_installed(provider.engine)
    plain = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="plain_probe",
            driver="sqlalchemy",
            options={"url": _admin_url()},
        )
    )
    assert not is_tenant_rls_installed(plain.engine)
