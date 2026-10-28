"""Prepare the benchmark database (idempotent).

1. create the database if missing;
2. apply Sandloom's CORE migration chain (``sandloom-migrate upgrade-core``) — the real
   ``outbox_messages`` table Sandloom's S4 writes to;
3. create the shared scenario tables and seed ``bench_items``;
4. create ``bare_outbox_messages`` as ``LIKE outbox_messages INCLUDING ALL`` —
   the bare apps' outbox has exactly Sandloom's columns, defaults and indexes;
5. S5: the ``TenantScoped`` table ``bench_tenant_items`` under FORCEd
   Row-Level Security (``enable_tenant_rls``), seeded for every tenant of
   ``common.S5_TENANTS``, the one-round-trip read function ``bench_s5_get``
   and the NON-superuser, NOBYPASSRLS application role ``common.S5_APP_ROLE``
   (DML-only grants) the S5 app connects as;
6. the write-path stand's ``TenantScoped`` tables under the same RLS
   (``write_statements``, DIAGNOSTIC ONLY; W4's counters seeded per tenant).

    python -m benchsuite.setup_db
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from benchsuite import common

DDL = f"""
CREATE TABLE IF NOT EXISTS bench_items (
    id BIGINT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bench_writes (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bare_outbox_messages (LIKE outbox_messages INCLUDING ALL);
INSERT INTO bench_items (id, name, value)
    SELECT g, 'item-' || g, g FROM generate_series(1, {common.SEED_ROWS}) AS g
    ON CONFLICT (id) DO NOTHING;
ANALYZE bench_items;
"""


def s5_statements(role: str, password: str) -> list[str]:
    """Idempotent DDL for S5 (table + RLS + seed + function + app role).

    Run as the database owner / a superuser; the app then connects as ``role``.
    ``role``/``password`` are inlined (DDL takes no bind parameters) — they come
    from the operator's environment, never from a request.
    """
    from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements

    if not role.isidentifier() or "'" in password:
        raise ValueError("invalid S5 role name / password")
    n = common.S5_ROWS_PER_TENANT
    tenants = ", ".join(f"({i}, '{t}'::uuid)" for i, t in enumerate(common.S5_TENANTS))
    return [
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
            CREATE ROLE {role} LOGIN PASSWORD '{password}'
              NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
          END IF;
        END $$""",
        f"ALTER ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS",
        """CREATE TABLE IF NOT EXISTS bench_tenant_items (
            id BIGINT PRIMARY KEY,
            tenant_id UUID NOT NULL,
            name VARCHAR(64) NOT NULL,
            value INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_bench_tenant_items_tenant_id "
        "ON bench_tenant_items (tenant_id)",
        # enable_tenant_rls is not idempotent (CREATE POLICY): drop it first.
        "DROP POLICY IF EXISTS bench_tenant_items_tenant_isolation "
        "ON bench_tenant_items",
        *enable_tenant_rls_statements("bench_tenant_items"),
        f"""INSERT INTO bench_tenant_items (id, tenant_id, name, value)
            SELECT t.i * {n} + g, t.tid, 'titem-' || (t.i * {n} + g), g
            FROM (VALUES {tenants}) AS t(i, tid), generate_series(1, {n}) AS g
            ON CONFLICT (id) DO NOTHING""",
        "ANALYZE bench_tenant_items",
        # S5 /s5fn: ONE round trip in autocommit. The GUC is set by a separate
        # plpgsql statement BEFORE the query runs (sequential, no evaluation-
        # order games). Hardening (audited):
        # * SECURITY INVOKER: runs with the CALLER's rights, so the FORCEd RLS
        #   policy applies to the NOBYPASSRLS app role inside it (DEFINER would
        #   run as the owner — a superuser here — and bypass RLS entirely);
        # * search_path pinned (pg_temp LAST: otherwise a caller's temp table
        #   named bench_tenant_items would shadow the real one) and the table
        #   schema-qualified;
        # * the previous GUC values are restored after the query, so calling it
        #   inside an open transaction cannot change that transaction's tenant
        #   (on an error the (sub)transaction rollback reverts set_config);
        # * EXECUTE revoked from PUBLIC (granted to the app role only, below).
        # The function TRUSTS p_tenant: the endpoint passes the tenant bound by
        # TenantScope (signed JWT claim; X-Tenant-Id only as a selector inside
        # the token's tenant_ids), never a client-chosen value.
        """CREATE OR REPLACE FUNCTION public.bench_s5_get(p_tenant uuid, p_id bigint)
        RETURNS TABLE (id bigint, name varchar, value integer)
        LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, public, pg_temp
        AS $fn$
        DECLARE
          prev_tenant text := current_setting('sandloom.tenant_id', true);
        BEGIN
          IF p_tenant IS NULL THEN
            RETURN;  -- fail closed: no tenant, no rows
          END IF;
          PERFORM set_config('sandloom.tenant_id', p_tenant::text, true);
          RETURN QUERY
            SELECT i.id, i.name, i.value FROM public.bench_tenant_items i
            WHERE i.id = p_id AND i.tenant_id = p_tenant;
          PERFORM set_config('sandloom.tenant_id', coalesce(prev_tenant, ''), true);
        END
        $fn$""",
        "REVOKE ALL ON FUNCTION public.bench_s5_get(uuid, bigint) FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role}",
        f"GRANT EXECUTE ON FUNCTION bench_s5_get(uuid, bigint) TO {role}",
    ]


#: Counter rows seeded per tenant for W4 (ids ``i*N + 1 .. (i+1)*N``).
WRITE_COUNTERS_PER_TENANT = 1_000
#: The write-path stand's tables (DIAGNOSTIC ONLY, ``bench.write_diag``).
WRITE_DIAG_TABLES = (
    "bench_tenant_order_lines",
    "bench_tenant_orders",
    "bench_tenant_writes",
    "bench_tenant_counters",
)


def write_statements(role: str) -> list[str]:
    """Idempotent DDL of the write-path stand (W1-W6, DIAGNOSTIC ONLY).

    Every table is ``TenantScoped`` under FORCEd RLS (the S5 policy), the
    counters are seeded per tenant, and the app ``role`` (created by
    :func:`s5_statements`, which must run first) gets DML on them. Run as the
    database owner / a superuser.
    """
    from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements

    if not role.isidentifier():
        raise ValueError("invalid S5 role name")
    n = WRITE_COUNTERS_PER_TENANT
    tenants = ", ".join(f"({i}, '{t}'::uuid)" for i, t in enumerate(common.S5_TENANTS))
    out = [
        """CREATE TABLE IF NOT EXISTS bench_tenant_writes (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL,
            name VARCHAR(64) NOT NULL,
            value INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bench_tenant_counters (
            id BIGINT PRIMARY KEY,
            tenant_id UUID NOT NULL,
            value INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bench_tenant_orders (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL,
            name VARCHAR(64) NOT NULL,
            total INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bench_tenant_order_lines (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL,
            order_id BIGINT NOT NULL REFERENCES bench_tenant_orders (id),
            sku VARCHAR(64) NOT NULL,
            qty INTEGER NOT NULL
        )""",
    ]
    for table in reversed(WRITE_DIAG_TABLES):
        out += [
            f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)",
            *enable_tenant_rls_statements(table),
        ]
    out += [
        "CREATE INDEX IF NOT EXISTS ix_bench_tenant_order_lines_order_id "
        "ON bench_tenant_order_lines (order_id)",
        f"""INSERT INTO bench_tenant_counters (id, tenant_id, value)
            SELECT t.i * {n} + g, t.tid, 0
            FROM (VALUES {tenants}) AS t(i, tid), generate_series(1, {n}) AS g
            ON CONFLICT (id) DO NOTHING""",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role}",
    ]
    return out


async def setup_s5(
    dsn: str | None = None, *, role: str | None = None, password: str | None = None
) -> None:
    conn = await asyncpg.connect(dsn or common.asyncpg_dsn())
    try:
        role = role or common.S5_APP_ROLE
        for sql in s5_statements(role, password or common.s5_app_password()):
            await conn.execute(sql)
        for sql in write_statements(role):
            await conn.execute(sql)
    finally:
        await conn.close()


#: Tables the writes land in; emptied before every server start.
WRITE_TABLES = ("bench_writes", "outbox_messages", "bare_outbox_messages")


async def _ensure_database(dsn: str) -> None:
    parts = urlsplit(dsn)
    dbname = parts.path.lstrip("/")
    admin_dsn = urlunsplit(parts._replace(path="/postgres"))
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname=$1", dbname
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


def _migrate_core() -> None:
    exe = shutil.which("sandloom-migrate", path=os.path.dirname(sys.executable))
    cmd = [exe] if exe else [sys.executable, "-m", "sandloom.core.cli.migrate"]
    env = {
        **os.environ,
        "DATABASE_URL": common.raw_database_url(),
        "CACHE_URL": common.cache_url(),
        "BROKER_URL": common.broker_url(),
        "JWT_SECRET": common.jwt_secret(),
        "ENVIRONMENT": "dev",
        "SANDLOOM_MODULES": '["benchsuite.bench"]',
        "LOG_LEVEL": "WARNING",
    }
    subprocess.run([*cmd, "upgrade-core", "--apply"], env=env, check=True)


async def truncate_writes(dsn: str | None = None) -> None:
    conn = await asyncpg.connect(dsn or common.asyncpg_dsn())
    try:
        await conn.execute(f"TRUNCATE {', '.join(WRITE_TABLES)} RESTART IDENTITY")
        # VACUUM cannot share a (implicit) transaction with other statements.
        await conn.execute("VACUUM ANALYZE bench_writes")
    finally:
        await conn.close()


async def count_writes(dsn: str | None = None) -> dict[str, int]:
    conn = await asyncpg.connect(dsn or common.asyncpg_dsn())
    try:
        counts = {
            t: int(await conn.fetchval(f"SELECT count(*) FROM {t}"))
            for t in WRITE_TABLES
        }
        # Rows Sandloom's post-commit dispatch / relay already delivered to the bus
        # (the bare outbox is written only — nobody relays it).
        counts["outbox_messages_delivered"] = int(
            await conn.fetchval(
                "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NOT NULL"
            )
        )
        return counts
    finally:
        await conn.close()


async def main() -> None:
    dsn = common.asyncpg_dsn()
    await _ensure_database(dsn)
    _migrate_core()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(DDL)
    finally:
        await conn.close()
    await setup_s5(dsn)
    await truncate_writes(dsn)
    print(f"benchmark database ready: {urlsplit(dsn).path.lstrip('/')}")


if __name__ == "__main__":
    asyncio.run(main())
