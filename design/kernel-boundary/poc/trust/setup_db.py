"""Create the PoC database objects (run as the container's superuser).

- role ``poc_app``: LOGIN, not superuser, no BYPASSRLS, not the table owner,
  so Postgres row security applies to the app's connections;
- ``notes_items`` with the kernel's own RLS DDL (``enable_tenant_rls_statements``);
- seed rows for tenants A and B.

Usage: python setup_db.py postgresql://poc:poc@127.0.0.1:38433/poc
"""

import asyncio
import sys

import asyncpg
from sqlalchemy.dialects import postgresql

from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements

T_A = "0a000000-0000-4000-8000-00000000000a"
T_B = "0b000000-0000-4000-8000-00000000000b"


async def main(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'poc_app'")
        if not exists:
            await conn.execute(
                "CREATE ROLE poc_app LOGIN PASSWORD 'poc_app' NOSUPERUSER NOBYPASSRLS"
            )
        await conn.execute("DROP TABLE IF EXISTS notes_items")
        await conn.execute(
            "CREATE TABLE notes_items (id uuid PRIMARY KEY, tenant_id uuid NOT NULL,"
            " body varchar(200) NOT NULL)"
        )
        for sql in enable_tenant_rls_statements(
            "notes_items", dialect=postgresql.dialect()
        ):
            print("RLS DDL:", sql.replace("\n", " ")[:160])
            await conn.execute(sql)
        await conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO poc_app")
        await conn.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON notes_items TO poc_app"
        )
        await conn.execute(
            "INSERT INTO notes_items VALUES (gen_random_uuid(), $1, 'note of A'),"
            " (gen_random_uuid(), $2, 'note of B')",
            T_A,
            T_B,
        )
        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'poc_app'"
        )
        print("poc_app superuser/bypassrls:", tuple(row))
    finally:
        await conn.close()


asyncio.run(main(sys.argv[1]))
