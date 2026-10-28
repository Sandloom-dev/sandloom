"""Seed the split topology's databases (one per service) for split_bench.

    python ladder/split_seed.py <base-dsn-without-db> <python-of-build>
    SPLIT_DB_PREFIX=bench_mine_split python ladder/split_seed.py …  # own DBs

Each ``bench_int_split_<service>`` database is created, migrated with its
service's module set (``sandloom-migrate upgrade-all`` of the measured build) and
seeded with the rows of ``mix seed-full``, each routed to the database that
owns its table. The dataset reset between runs is ``benchsuite.dataset``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys

import asyncpg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from benchsuite import mix  # noqa: E402

#: Database name prefix (``<prefix>_<service>``).
PREFIX = os.environ.get("SPLIT_DB_PREFIX", "bench_int_split")


class _Router:
    """asyncpg-like: sends each statement to the database owning its table."""

    def __init__(self, conns: dict[str, asyncpg.Connection], owner: dict[str, str]):
        self.conns, self.owner = conns, owner

    def _pick(self, sql: str) -> asyncpg.Connection:
        m = re.search(r"(?:INTO|FROM|UPDATE)\s+(\w+)", sql)
        assert m, sql
        return self.conns[self.owner[m.group(1)]]

    async def execute(self, sql, *a):
        return await self._pick(sql).execute(sql, *a)

    async def executemany(self, sql, rows):
        return await self._pick(sql).executemany(sql, rows)

    async def fetchval(self, sql, *a):
        return await self._pick(sql).fetchval(sql, *a)


SPLIT = {  # service -> modules (deploy/compose/microservices.yml + examples)
    "identity": ["sandloom_identity"],
    "tenancy": ["sandloom_tenancy"],
    "billing": ["sandloom_billing"],
    "apps": mix.FULL_MODULES[3:],
}


async def split_seed(base: str, python: str) -> None:
    """base: postgresql://user:pw@host:port (no db). One DB per service."""
    examples = mix.EXAMPLES
    conns = {}
    owner: dict[str, str] = {}
    for svc, modules in SPLIT.items():
        db = f"{PREFIX}_{svc}"
        admin = await asyncpg.connect(f"{base}/postgres")
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", db):
            await admin.execute(f'CREATE DATABASE "{db}"')
        await admin.close()
        c = await asyncpg.connect(
            f"{base}/{db}"
        )  # as deploy/postgres/init-databases.sh
        await c.execute("CREATE EXTENSION IF NOT EXISTS citext")
        await c.close()
        env = {
            **os.environ,
            **mix.server_env(full=True),
            "SANDLOOM_MODULES": json.dumps(modules),
            "DATABASE_URL": base.replace("postgresql://", "postgresql+asyncpg://")
            + f"/{db}?ssl=disable",
            "PYTHONPATH": os.pathsep.join(
                os.path.join(examples, a) for a in ("helpdesk", "projecthub", "linkly")
            ),
        }
        exe = os.path.join(os.path.dirname(python), "sandloom-migrate")
        subprocess.run([exe, "upgrade-all", "--apply"], env=env, check=True)
        conns[svc] = await asyncpg.connect(f"{base}/{db}")
        for r in await conns[svc].fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ):
            if r[0] not in ("outbox_messages", "audit_log", "consumer_inbox") and not r[
                0
            ].startswith("alembic"):
                owner[r[0]] = svc
    router = _Router(conns, owner)
    if not await conns["identity"].fetchval(
        "SELECT 1 FROM users WHERE id=$1", mix.USER
    ):
        await conns["identity"].execute(
            """INSERT INTO users (id, email, full_name, is_active, is_superuser,
                   is_verified, created_at, updated_at)
               VALUES ($1, 'mix@example.com', 'Mix User', true, false, true, now(), now())""",
            mix.USER,
        )
        await conns["tenancy"].execute(
            """INSERT INTO tenancy_organizations
                   (id, name, slug, owner_id, is_active, created_at, updated_at)
               VALUES ($1, 'Mix Org', 'mix-org', $2, true, now(), now())""",
            mix.ORG,
            mix.USER,
        )
        import uuid

        await conns["tenancy"].execute(
            """INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
               VALUES ($1, $2, $3, 'owner', now())""",
            uuid.uuid4(),
            mix.ORG,
            mix.USER,
        )
        await mix._seed_full(router)  # type: ignore[arg-type]
    for c in conns.values():
        await c.close()
    print("split databases ready:", ", ".join(f"{PREFIX}_{s}" for s in SPLIT))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    asyncio.run(split_seed(sys.argv[1], sys.argv[2]))
