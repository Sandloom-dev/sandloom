"""Tenant RLS vs an attacker who can send arbitrary SQL as the app role.

Real PostgreSQL. Roles mirror a hardened production layout:

* the OWNER of the tables is the migration role (here the superuser test role);
* the APPLICATION role is a separate NOSUPERUSER NOBYPASSRLS login with DML
  grants only — what DbSession / db_session() / contract sessions connect as;
* the SYSTEM role is another separate NOSUPERUSER NOBYPASSRLS login, granted
  cross-tenant access per table by ``enable_tenant_rls(system_access=True)``.

Before the fix the policy accepted ``sandloom.tenant_bypass = 'on'``, a custom
setting any role may set, so raw SQL (``exec_driver_sql``, ``text()``, a driver
connection) could read/write every tenant. These tests pin that the bypass is
gone for SELECT / INSERT / UPDATE / DELETE, that a malformed tenant setting
fails closed (no rows, refused writes — never a cast error), that the system
channel works and is audited, and that startup refuses an app role that could
escape the policies. Forging ``sandloom.tenant_id`` itself remains possible — pinned
as a strict xfail documenting the residual risk (security-model.md).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sandloom.core.di import bind_tenant, unscoped
from sandloom.core.drivers.sqlalchemy_database import (
    SQLAlchemyDatabaseProvider,
    SystemChannelUnavailable,
    create_sqlalchemy_database,
    system_channel_configured,
)
from sandloom.core.drivers.system_db import (
    AUDIT_ACTION,
    SystemSessionRefused,
    system_session,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.sql_alchemy.tenant_rls import (
    TenantRLSConfigError,
    install_tenant_rls,
    verify_tenant_rls_roles,
)
from sandloom.core.testing.workers import reload_test_env

A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


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
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    table: str
    plain_table: str
    app_role: str
    sys_role: str
    app_url: str
    sys_url: str
    app: AsyncEngine
    admin: AsyncEngine
    instance: str
    provider: SQLAlchemyDatabaseProvider

    async def names(self, conn: Any, table: str | None = None) -> list[str]:
        rows = await conn.execute(
            text(f"SELECT name FROM {table or self.table} ORDER BY name")
        )
        return [r[0] for r in rows]


def _url(admin_url: str, role: str, password: str) -> str:
    return (
        make_url(admin_url)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


@pytest_asyncio.fixture
async def env() -> AsyncGenerator[_Env, None]:
    admin_url = _admin_url()
    sfx = uuid.uuid4().hex[:10]
    table, plain = f"test_rlsx_{sfx}", f"test_rlsx_plain_{sfx}"
    app_role, sys_role = f"sandloom_rlsx_app_{sfx}", f"sandloom_rlsx_sys_{sfx}"
    app_pw, sys_pw = uuid.uuid4().hex, uuid.uuid4().hex
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.run_sync(
            lambda c: AuditLogEntry.__table__.create(c, checkfirst=True)  # type: ignore[attr-defined]
        )
        for role, pw in ((app_role, app_pw), (sys_role, sys_pw)):
            await conn.execute(
                text(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' "
                    "NOSUPERUSER NOBYPASSRLS NOCREATEROLE"
                )
            )
        for t in (table, plain):
            await conn.execute(
                text(
                    f"CREATE TABLE {t} (id uuid PRIMARY KEY DEFAULT "
                    "gen_random_uuid(), tenant_id uuid NOT NULL, name text NOT NULL)"
                )
            )
            await conn.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {app_role}")
            )
            await conn.execute(
                text(
                    f"INSERT INTO {t} (tenant_id, name) VALUES "
                    f"('{A}', 'a1'), ('{A}', 'a2'), ('{B}', 'b1')"
                )
            )
        # `table` grants the system channel cross-tenant access; `plain` not.
        for sql in enable_tenant_rls_statements(
            table, system_access=True, system_role=sys_role
        ):
            await conn.execute(text(sql))
        for sql in enable_tenant_rls_statements(plain):
            await conn.execute(text(sql))
        await conn.execute(text(f"GRANT SELECT ON {plain} TO {sys_role}"))
        await conn.execute(text(f"GRANT INSERT ON audit_log TO {sys_role}"))
    app_url, sys_url = (
        _url(admin_url, app_role, app_pw),
        _url(admin_url, sys_role, sys_pw),
    )
    # pool_size=1: every transaction reuses ONE physical connection, so any
    # setting leaking across transactions/tenants would show up.
    app = create_async_engine(app_url, pool_size=1, max_overflow=0)
    install_tenant_rls(app)
    instance = f"rlsx_{sfx}"
    provider = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name=instance,
            driver="sqlalchemy",
            options={"url": app_url, "tenant_rls": True, "system_url": sys_url},
        )
    )
    e = _Env(
        table=table,
        plain_table=plain,
        app_role=app_role,
        sys_role=sys_role,
        app_url=app_url,
        sys_url=sys_url,
        app=app,
        admin=admin,
        instance=instance,
        provider=provider,
    )
    try:
        yield e
    finally:
        await app.dispose()
        await provider.aclose()
        async with admin.begin() as conn:
            await conn.execute(
                text("DELETE FROM audit_log WHERE action = :a AND resource_id = :r"),
                {"a": AUDIT_ACTION, "r": instance},
            )
            for t in (table, plain):
                await conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
            for role in (app_role, sys_role):
                await conn.execute(text(f"DROP OWNED BY {role}"))
                await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


# --- (a) the bypass setting no longer opens anything ----------------------


@pytest.mark.asyncio
async def test_bypass_setting_cannot_read_other_tenants(env: _Env) -> None:
    with bind_tenant(A):
        async with env.app.begin() as conn:
            await conn.exec_driver_sql(
                "SELECT set_config('sandloom.tenant_bypass', 'on', true)"
            )
            assert await env.names(conn) == ["a1", "a2"]
    # Raw engine without the hook and without any tenant: still nothing.
    raw = create_async_engine(env.app_url)
    try:
        async with raw.begin() as conn:
            await conn.exec_driver_sql("SET sandloom.tenant_bypass = 'on'")
            assert await env.names(conn) == []
    finally:
        await raw.dispose()


@pytest.mark.asyncio
async def test_bypass_setting_cannot_write_other_tenants(env: _Env) -> None:
    with bind_tenant(A):
        async with env.app.begin() as conn:
            await conn.exec_driver_sql(
                "SELECT set_config('sandloom.tenant_bypass', 'on', true)"
            )
            upd = await conn.execute(
                text(f"UPDATE {env.table} SET name = 'pwn' WHERE name = 'b1'")
            )
            assert upd.rowcount == 0  # type: ignore[attr-defined]
            dele = await conn.execute(
                text(f"DELETE FROM {env.table} WHERE name = 'b1'")
            )
            assert dele.rowcount == 0  # type: ignore[attr-defined]
        with pytest.raises(DBAPIError, match="row-level security"):
            async with env.app.begin() as conn:
                await conn.exec_driver_sql(
                    "SELECT set_config('sandloom.tenant_bypass', 'on', true)"
                )
                await conn.execute(
                    text(
                        f"INSERT INTO {env.table} (tenant_id, name) "
                        f"VALUES ('{B}', 'planted')"
                    )
                )
    async with env.admin.connect() as conn:
        assert await env.names(conn) == ["a1", "a2", "b1"]


@pytest.mark.asyncio
async def test_unscoped_never_reaches_other_tenants_on_app_role(env: _Env) -> None:
    with unscoped():
        async with env.app.begin() as conn:
            assert await env.names(conn) == []
            upd = await conn.execute(text(f"UPDATE {env.table} SET name = name"))
            assert upd.rowcount == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unscoped_without_tenant_warns_once(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="sandloom.tenant_rls"):
        with unscoped():
            async with env.app.begin() as conn:
                await env.names(conn)
                await env.names(conn)
    hits = [r for r in caplog.records if "does not lift Row-Level" in r.getMessage()]
    assert len(hits) == 1


# --- (b) forging the tenant setting itself: residual risk -----------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="tenant context integrity is NOT guaranteed: the app role can set "
    "sandloom.tenant_id to any tenant (documented residual risk, security-model.md)",
)
async def test_forged_tenant_setting_cannot_read_other_tenant(env: _Env) -> None:
    with bind_tenant(A):
        async with env.app.begin() as conn:
            await conn.exec_driver_sql(
                f"SELECT set_config('sandloom.tenant_id', '{B}', true)"
            )
            assert "b1" not in await env.names(conn)


# --- (c) empty / malformed tenant setting fails closed ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["", "not-a-uuid", "' OR 1=1 --", f"{{{A}}}", str(A).upper(), str(A) + "x"],
)
async def test_malformed_tenant_setting_fails_closed(env: _Env, value: str) -> None:
    raw = create_async_engine(env.app_url)
    try:
        async with raw.begin() as conn:
            await conn.execute(
                text("SELECT set_config('sandloom.tenant_id', :v, true)"), {"v": value}
            )
            assert await env.names(conn) == []
            upd = await conn.execute(text(f"UPDATE {env.table} SET name = name"))
            assert upd.rowcount == 0  # type: ignore[attr-defined]
        with pytest.raises(DBAPIError, match="row-level security"):
            async with raw.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('sandloom.tenant_id', :v, true)"),
                    {"v": value},
                )
                await conn.execute(
                    text(
                        f"INSERT INTO {env.table} (tenant_id, name) VALUES ('{A}', 'x')"
                    )
                )
    finally:
        await raw.dispose()


# --- honest path: all four verbs scoped, pooled reuse does not leak ---------


@pytest.mark.asyncio
async def test_crud_is_tenant_scoped_and_pool_reuse_does_not_leak(env: _Env) -> None:
    with bind_tenant(A):
        async with env.app.begin() as conn:
            await conn.execute(
                text(f"INSERT INTO {env.table} (tenant_id, name) VALUES ('{A}', 'a3')")
            )
            await conn.execute(
                text(f"UPDATE {env.table} SET name = 'a3u' WHERE name = 'a3'")
            )
            assert await env.names(conn) == ["a1", "a2", "a3u"]
    # Same physical connection (pool_size=1), other tenant, then no tenant.
    with bind_tenant(B):
        async with env.app.begin() as conn:
            assert await env.names(conn) == ["b1"]
            dele = await conn.execute(
                text(f"DELETE FROM {env.table} WHERE name = 'a3u'")
            )
            assert dele.rowcount == 0  # type: ignore[attr-defined]
    async with env.app.begin() as conn:
        assert await env.names(conn) == []
    with bind_tenant(A):
        async with env.app.begin() as conn:
            dele = await conn.execute(
                text(f"DELETE FROM {env.table} WHERE name = 'a3u'")
            )
            assert dele.rowcount == 1  # type: ignore[attr-defined]


# --- system channel ---------------------------------------------------------


@pytest.mark.asyncio
async def test_system_channel_reaches_all_tenants_and_is_audited(env: _Env) -> None:
    assert system_channel_configured(env.instance)
    async with system_session(
        actor="test:ops", reason="cross-tenant repair test", database=env.instance
    ) as s:
        assert await env.names(s) == ["a1", "a2", "b1"]
        await s.execute(
            text(f"INSERT INTO {env.table} (tenant_id, name) VALUES ('{B}', 'b2')")
        )
        upd = await s.execute(text(f"UPDATE {env.table} SET name = name"))
        assert upd.rowcount == 4  # type: ignore[attr-defined]
        await s.execute(text(f"DELETE FROM {env.table} WHERE name = 'b2'"))
        # A table WITHOUT system_access stays tenant-scoped for the system role.
        assert await env.names(s, env.plain_table) == []
    async with env.admin.connect() as conn:
        rows = (
            await conn.execute(
                select(AuditLogEntry.data).where(
                    AuditLogEntry.action == AUDIT_ACTION,
                    AuditLogEntry.resource_id == env.instance,
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0][0]["actor"] == "test:ops"
    assert rows[0][0]["reason"] == "cross-tenant repair test"


@pytest.mark.asyncio
async def test_system_session_tenant_scoped_and_rollback(env: _Env) -> None:
    async with system_session(
        actor="test:ops", reason="single tenant job", database=env.instance, tenant_id=B
    ) as s:
        assert await env.names(s, env.plain_table) == ["b1"]
    with pytest.raises(RuntimeError, match="boom"):
        async with system_session(
            actor="test:ops", reason="rolled back job", database=env.instance
        ) as s:
            await s.execute(text(f"DELETE FROM {env.table}"))
            raise RuntimeError("boom")
    async with env.admin.connect() as conn:
        assert await env.names(conn) == ["a1", "a2", "b1"]


@pytest.mark.asyncio
async def test_system_session_requires_actor_reason_and_configuration(
    env: _Env,
) -> None:
    with pytest.raises(SystemSessionRefused):
        async with system_session(actor="", reason="long enough reason"):
            pass
    with pytest.raises(SystemSessionRefused):
        async with system_session(actor="ops", reason="short"):
            pass
    with pytest.raises(SystemChannelUnavailable, match="no system channel"):
        async with system_session(
            actor="ops", reason="not configured db", database="nope_" + env.instance
        ):
            pass


@pytest.mark.asyncio
async def test_app_role_cannot_assume_system_role(env: _Env) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with env.app.begin() as conn:
            await conn.exec_driver_sql(f"SET ROLE {env.sys_role}")


# --- startup verification ---------------------------------------------------


@pytest.mark.asyncio
async def test_verification_accepts_hardened_roles(env: _Env) -> None:
    await env.provider.aopen()


@pytest.mark.asyncio
async def test_verification_refuses_superuser_app_role(env: _Env) -> None:
    async with env.admin.connect() as conn:
        with pytest.raises(TenantRLSConfigError, match="SUPERUSER"):
            await verify_tenant_rls_roles(conn)


@pytest.mark.asyncio
async def test_verification_refuses_owner_and_member(env: _Env) -> None:
    async with env.admin.begin() as conn:
        await conn.execute(
            text(f"ALTER TABLE {env.plain_table} OWNER TO {env.app_role}")
        )
    async with env.app.connect() as conn:
        with pytest.raises(TenantRLSConfigError, match="owns RLS table"):
            await verify_tenant_rls_roles(conn)
    async with env.admin.begin() as conn:
        await conn.execute(text(f"ALTER TABLE {env.plain_table} OWNER TO CURRENT_USER"))
        # The ownership round trip folded the app role's grants into the owner's.
        await conn.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {env.plain_table} "
                f"TO {env.app_role}"
            )
        )
        await conn.execute(text(f"GRANT {env.sys_role} TO {env.app_role}"))
    async with env.app.connect() as conn:
        with pytest.raises(TenantRLSConfigError, match="unconditional policies"):
            await verify_tenant_rls_roles(conn)
    async with env.admin.begin() as conn:
        await conn.execute(text(f"REVOKE {env.sys_role} FROM {env.app_role}"))


@pytest.mark.asyncio
async def test_verification_failure_aborts_startup_in_every_environment() -> None:
    from sandloom.core.infra.registry import InfraRegistry, InfraStartupError

    class _Bad:
        async def aopen(self) -> None:
            raise TenantRLSConfigError("app role is a SUPERUSER")

    reg = InfraRegistry(config={"database": {"default": None}})  # type: ignore[dict-item]
    reg.provider = lambda *_a, **_k: _Bad()  # type: ignore[method-assign,assignment,return-value]
    with pytest.raises(InfraStartupError, match="SUPERUSER"):
        await reg.aopen_all(strict=False)


def test_require_system_channel_without_dsn_fails_fast() -> None:
    with pytest.raises(SystemChannelUnavailable, match="require_system_channel"):
        create_sqlalchemy_database(
            InstanceConfig(
                capability="database",
                name="rls_required_probe",
                driver="sqlalchemy",
                options={
                    "url": _admin_url(),
                    "tenant_rls": True,
                    "require_system_channel": True,
                },
            )
        )
    with pytest.raises(TenantRLSConfigError, match="tenant_rls is off"):
        create_sqlalchemy_database(
            InstanceConfig(
                capability="database",
                name="rls_off_probe",
                driver="sqlalchemy",
                options={"url": _admin_url(), "system_url": _admin_url()},
            )
        )


@pytest.mark.asyncio
async def test_rls_off_instance_is_unchanged() -> None:
    # No tenant_rls: no hook, no system channel, aopen does no I/O (the URL is
    # never connected to), DATABASE_SYSTEM_URL is ignored.
    provider = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="rls_off_unchanged",
            driver="sqlalchemy",
            options={"url": "postgresql+asyncpg://u:p@127.0.0.1:1/never"},
        )
    )
    await provider.aopen()
    assert not system_channel_configured("rls_off_unchanged")
    assert "tenant_rls" not in provider.features()
    await provider.aclose()


def test_removed_verify_roles_option_is_refused() -> None:
    # The role check always runs; the former escape hatch fails startup.
    with pytest.raises(ValueError, match="verify_roles was removed in 0.8"):
        create_sqlalchemy_database(
            InstanceConfig(
                capability="database",
                name="nv",
                driver="sqlalchemy",
                options={
                    "url": "postgresql+asyncpg://u:p@h/d",
                    "tenant_rls": True,
                    "verify_roles": False,
                },
            )
        )
