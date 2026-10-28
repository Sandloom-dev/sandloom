"""The stand's dataset reset (``benchsuite.dataset``) returns a database to
its seeded state: row counts are IDENTICAL at the start of every run.

Runs against a scratch database ``<DATABASE_URL db>_harness_dataset`` with
minimal versions of the tables the mix writes to (the same names and the
columns the reset statements use), seeded like ``mix seed-full``.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Iterator

import pytest

from benchsuite import dataset

asyncpg = pytest.importorskip("asyncpg")

TABLES = """
CREATE TABLE tickets_tickets (id uuid PRIMARY KEY, created_at timestamptz NOT NULL);
CREATE TABLE tasks_tasks (id uuid PRIMARY KEY, created_at timestamptz NOT NULL);
CREATE TABLE taskboard_project_task_query_index (task_id uuid PRIMARY KEY);
CREATE TABLE notifications_inapp (id uuid PRIMARY KEY, created_at timestamptz NOT NULL);
CREATE TABLE links_shortlinks (id uuid PRIMARY KEY, code text UNIQUE NOT NULL);
CREATE TABLE outbox_messages (id uuid PRIMARY KEY);
CREATE TABLE audit_log (id uuid PRIMARY KEY);
CREATE TABLE consumer_inbox (id uuid PRIMARY KEY);
CREATE TABLE refresh_tokens (id uuid PRIMARY KEY);
CREATE TABLE users (id uuid PRIMARY KEY);
CREATE TABLE alembic_version_x (version_num text);
"""
LISTS = ("tickets_tickets", "tasks_tasks", "notifications_inapp")


def _base_dsn() -> str | None:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return re.sub(r"([?&])ssl=", r"\1sslmode=", url)


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    base = _base_dsn()
    if base is None:
        if os.environ.get("SANDLOOM_E2E_REQUIRED") == "1":
            pytest.fail("SANDLOOM_E2E_REQUIRED=1 but no PostgreSQL DATABASE_URL")
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    head, _, tail = base.rpartition("/")
    name, _, query = tail.partition("?")
    db = f"{name}_harness_dataset"
    admin_dsn = f"{head}/postgres" + (f"?{query}" if query else "")
    target = f"{head}/{db}" + (f"?{query}" if query else "")

    async def setup() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
            await admin.execute(f'CREATE DATABASE "{db}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(target)
        try:
            await conn.execute(TABLES)
            await _seed(conn)
        finally:
            await conn.close()

    async def teardown() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        finally:
            await admin.close()

    try:
        asyncio.run(setup())
    except (OSError, asyncpg.PostgresError) as exc:
        if os.environ.get("SANDLOOM_E2E_REQUIRED") == "1":
            raise
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    yield target
    asyncio.run(teardown())


async def _seed(conn: asyncpg.Connection) -> None:
    """As ``mix._seed_full``: LIST_ROWS rows per list, one link, one user."""
    for t in LISTS:
        await conn.executemany(
            f"INSERT INTO {t} (id, created_at) VALUES ($1, now() - interval '1 hour')",
            [(uuid.uuid4(),) for _ in range(dataset.KEEP)],
        )
    await conn.execute(
        "INSERT INTO taskboard_project_task_query_index SELECT id FROM tasks_tasks"
    )
    await conn.execute(
        "INSERT INTO links_shortlinks VALUES ($1, $2)", uuid.uuid4(), dataset.LINK_CODE
    )
    await conn.execute("INSERT INTO users VALUES ($1)", uuid.uuid4())


async def _benchmark_writes(conn: asyncpg.Connection, k: int) -> None:
    """What a run leaves behind: new list rows, projection rows, links,
    outbox / audit / inbox / refresh-token rows."""
    for t in LISTS:
        await conn.executemany(
            f"INSERT INTO {t} (id, created_at) VALUES ($1, now())",
            [(uuid.uuid4(),) for _ in range(7 * k)],
        )
    await conn.execute(
        "INSERT INTO taskboard_project_task_query_index "
        "SELECT id FROM tasks_tasks ON CONFLICT DO NOTHING"
    )
    await conn.executemany(
        "INSERT INTO links_shortlinks VALUES ($1, $2)",
        [(uuid.uuid4(), f"m{uuid.uuid4().hex[:8]}") for _ in range(5 * k)],
    )
    for t in ("outbox_messages", "audit_log", "consumer_inbox", "refresh_tokens"):
        await conn.executemany(
            f"INSERT INTO {t} VALUES ($1)", [(uuid.uuid4(),) for _ in range(3 * k)]
        )


def test_reset_restores_identical_row_counts(dsn: str) -> None:
    async def scenario() -> None:
        await dataset.reset([dsn])
        baseline = await dataset.row_counts([dsn])
        (counts,) = baseline.values()
        assert "alembic_version_x" not in counts
        assert {counts[t] for t in LISTS} == {dataset.KEEP}
        assert counts["taskboard_project_task_query_index"] == dataset.KEEP
        assert counts["links_shortlinks"] == 1 and counts["outbox_messages"] == 0
        for k in (1, 2, 3):  # three "runs"
            conn = await asyncpg.connect(dsn)
            try:
                await _benchmark_writes(conn, k)
            finally:
                await conn.close()
            grown = await dataset.row_counts([dsn])
            assert dataset.diff_counts(baseline, grown)  # the writes are visible
            await dataset.reset([dsn])
            assert dataset.diff_counts(baseline, await dataset.row_counts([dsn])) == {}

    asyncio.run(scenario())


def test_counts_catch_a_table_the_reset_does_not_cover(dsn: str) -> None:
    """Negative control: a growing table outside RESET_SQL is reported."""

    async def scenario() -> None:
        await dataset.reset([dsn])
        baseline = await dataset.row_counts([dsn])
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("INSERT INTO users VALUES ($1)", uuid.uuid4())
            await dataset.reset([dsn])
            diff = dataset.diff_counts(baseline, await dataset.row_counts([dsn]))
            (db,) = diff
            assert diff[db] == {"users": (1, 2)}
            await conn.execute(
                "DELETE FROM users WHERE id NOT IN (SELECT id FROM users LIMIT 1)"
            )
        finally:
            await conn.close()

    asyncio.run(scenario())
