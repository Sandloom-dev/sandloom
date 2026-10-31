"""Return a stand's databases to the SEEDED state before every run.

Without it the write requests of one run (tickets / tasks / links / outbox /
audit rows ...) stay in the tables, so every later run — and every later
configuration of an interleaved round — reads longer lists than the first.

* :func:`reset` keeps the first ``mix.LIST_ROWS`` rows of every example list
  (the seed), drops everything written since (rows, outbox, audit, inbox,
  refresh tokens) and ``VACUUM ANALYZE``\\s what it touched. Tables a database
  does not have are skipped (the split topology has one database per service).
* :func:`row_counts` snapshots every public table's row count; the run driver
  records it at each run start and marks the run invalid when it differs from
  the database's first snapshot (``benchmarks/tests/test_harness_dataset.py``
  pins that a reset after writes reproduces the snapshot exactly).
* :func:`flush_redis` empties the run's Redis dbs and returns their sizes
  afterwards (all 0, or the run is invalid).

    python -m benchsuite.dataset reset <asyncpg-dsn> [<dsn> ...]
    python -m benchsuite.dataset counts <asyncpg-dsn> [<dsn> ...]
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import asyncpg

#: seeded rows kept per example list (= benchsuite.mix.LIST_ROWS; literal so
#: this module does not import the Sandloom settings stack)
KEEP = 30
LINK_CODE = "mixcode"

#: (table, statement) in dependency order; a table missing from a database is
#: skipped. Ordering by (created_at, id) keeps exactly the seeded rows: the
#: seed inserts them first, every benchmark write comes later.
RESET_SQL: list[tuple[str, str]] = [
    (
        "tickets_tickets",
        "DELETE FROM tickets_tickets WHERE id NOT IN (SELECT id FROM "
        f"tickets_tickets ORDER BY created_at, id LIMIT {KEEP})",
    ),
    (
        "tasks_tasks",
        "DELETE FROM tasks_tasks WHERE id NOT IN (SELECT id FROM tasks_tasks "
        f"ORDER BY created_at, id LIMIT {KEEP})",
    ),
    (
        "taskboard_project_task_query_index",
        "DELETE FROM taskboard_project_task_query_index "
        "WHERE task_id NOT IN (SELECT id FROM tasks_tasks)",
    ),
    (
        "notifications_inapp",
        "DELETE FROM notifications_inapp WHERE id NOT IN (SELECT id FROM "
        f"notifications_inapp ORDER BY created_at, id LIMIT {KEEP})",
    ),
    ("links_shortlinks", f"DELETE FROM links_shortlinks WHERE code <> '{LINK_CODE}'"),
    ("outbox_messages", "TRUNCATE outbox_messages"),
    ("audit_log", "TRUNCATE audit_log"),
    ("consumer_inbox", "TRUNCATE consumer_inbox"),
    ("contract_command_log", "TRUNCATE contract_command_log"),
    ("http_idempotency_log", "TRUNCATE http_idempotency_log"),
    ("refresh_tokens", "DELETE FROM refresh_tokens"),
]
#: a statement that reads another table runs only when that table exists too
_NEEDS = {"taskboard_project_task_query_index": "tasks_tasks"}


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    return {r[0] for r in rows}


def _table_identifier(name: str) -> str:
    """Quote a PostgreSQL catalog identifier, including embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


async def reset_one(conn: asyncpg.Connection) -> list[str]:
    have = await _tables(conn)
    done = []
    for table, sql in RESET_SQL:
        if table in have and _NEEDS.get(table, table) in have:
            await conn.execute(sql)
            done.append(table)
    for table in done:
        await conn.execute(f"VACUUM ANALYZE {_table_identifier(table)}")
    return done


async def counts_one(conn: asyncpg.Connection) -> dict[str, int]:
    out = {}
    for t in sorted(await _tables(conn)):
        if t.startswith("alembic_version"):
            continue
        out[t] = await conn.fetchval(f"SELECT count(*) FROM {_table_identifier(t)}")
    return out


async def reset(dsns: list[str]) -> None:
    for dsn in dsns:
        conn = await asyncpg.connect(dsn)
        try:
            await reset_one(conn)
        finally:
            await conn.close()


async def row_counts(dsns: list[str]) -> dict[str, dict[str, int]]:
    """``{dsn database name: {table: rows}}``."""
    out = {}
    for dsn in dsns:
        conn = await asyncpg.connect(dsn)
        try:
            out[dsn.rsplit("/", 1)[-1].split("?")[0]] = await counts_one(conn)
        finally:
            await conn.close()
    return out


def flush_redis(container: str, dbs: list[int]) -> dict[int, int]:
    """FLUSHDB each db in a Redis container; returns DBSIZE after (0 = clean)."""
    sizes = {}
    for db in dbs:
        base = ["docker", "exec", container, "redis-cli", "-n", str(db)]
        subprocess.run([*base, "FLUSHDB"], check=True, capture_output=True)
        out = subprocess.run(
            [*base, "DBSIZE"], check=True, capture_output=True, text=True
        ).stdout
        sizes[db] = int(out.split()[-1])
    return sizes


def diff_counts(
    baseline: dict[str, dict[str, int]], now: dict[str, dict[str, int]]
) -> dict[str, dict[str, tuple[int | None, int | None]]]:
    """Tables whose row count differs from the baseline: {db: {t: (was, now)}}."""
    out: dict[str, dict[str, tuple[int | None, int | None]]] = {}
    for db in set(baseline) | set(now):
        a, b = baseline.get(db, {}), now.get(db, {})
        d = {t: (a.get(t), b.get(t)) for t in set(a) | set(b) if a.get(t) != b.get(t)}
        if d:
            out[db] = d
    return out


if __name__ == "__main__":
    cmd, dsns = sys.argv[1], sys.argv[2:]
    if cmd == "reset":
        asyncio.run(reset(dsns))
    elif cmd == "counts":
        print(json.dumps(asyncio.run(row_counts(dsns)), indent=1))
    else:
        raise SystemExit(__doc__)
