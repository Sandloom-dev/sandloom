"""The migration connection runs with a PostgreSQL ``lock_timeout`` (and an
optional ``statement_timeout``): DDL queued behind a long transaction fails
instead of holding its lock request and stalling every query on the table.

Each test runs a real one-revision chain through ``run_migrations`` on a
scratch database; they skip where the test database takes its schema from the
models and no chain runs (SANDLOOM-B028).
"""

from __future__ import annotations

import os
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from sandloom.core.config.settings import settings
from sandloom.core.migrations.pg_sync import psycopg2
from sandloom.core.testing.migrations import (
    run_sql,
    runs_no_chains,
    temporary_database,
)

pytestmark = pytest.mark.skipif(
    runs_no_chains(os.environ.get("DATABASE_URL", "")),
    reason="needs PostgreSQL: SQLite runs no chain (SANDLOOM-B028)",
)


@pytest.fixture
def scratch_database() -> Iterator[str]:
    with temporary_database() as url:
        yield url


_ENV = """\
from sandloom.core.migrations.alembic_support import run_migrations

run_migrations()
"""


def _chain_of_file(
    root: Path, revision_file: str, version_table: str = "alembic_version_ddltimeouts"
) -> Config:
    """A chain of one revision whose file content is ``revision_file``."""
    (root / "versions").mkdir(parents=True)
    (root / "env.py").write_text(_ENV)
    (root / "versions" / "0001_probe.py").write_text(revision_file)
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    cfg.set_main_option("version_table", version_table)
    return cfg


def _chain(root: Path, upgrade_body: str) -> Config:
    """A chain of one revision whose ``upgrade()`` runs ``upgrade_body``."""
    return _chain_of_file(
        root,
        "from alembic import op\n"
        "revision = '0001'\n"
        "down_revision = None\n\n\n"
        "def upgrade() -> None:\n"
        + textwrap.indent(textwrap.dedent(upgrade_body), "    ")
        + "\n\n\ndef downgrade() -> None:\n    pass\n",
    )


def _upgrade(cfg: Config, url: str) -> None:
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


_RECORD = """\
bind = op.get_bind()
op.execute("CREATE TABLE seen (name text, value text)")
for name in ("lock_timeout", "statement_timeout"):
    value = bind.exec_driver_sql(f"SHOW {name}").scalar()
    op.execute(f"INSERT INTO seen VALUES ('{name}', '{value}')")
"""


def _seen(url: str) -> dict[str, str]:
    return dict(run_sql(url, "SELECT * FROM seen"))


def test_the_migration_connection_carries_the_configured_timeouts(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 1234)
    monkeypatch.setattr(settings, "migration_statement_timeout_ms", 56_000)

    _upgrade(_chain(tmp_path / "chain", _RECORD), scratch_database)

    assert _seen(scratch_database) == {
        "lock_timeout": "1234ms",
        "statement_timeout": "56s",
    }


def test_the_defaults_bound_ddl_waits_and_leave_statements_unbounded(
    tmp_path: Path, scratch_database: str
) -> None:
    _upgrade(_chain(tmp_path / "chain", _RECORD), scratch_database)

    assert _seen(scratch_database) == {
        "lock_timeout": "5s",
        "statement_timeout": "0",
    }


def test_zero_overrides_a_server_default(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_sql(scratch_database, "SELECT 1")
    dbname = run_sql(scratch_database, "SELECT current_database()")[0][0]
    # a database-level default the zero must override, not inherit
    admin = psycopg2.connect(scratch_database)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'ALTER DATABASE "{dbname}" SET lock_timeout = 777')
    finally:
        admin.close()
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 0)

    _upgrade(_chain(tmp_path / "chain", _RECORD), scratch_database)

    assert _seen(scratch_database)["lock_timeout"] == "0"


def test_a_revision_lifts_the_lock_timeout_for_its_own_transaction(
    tmp_path: Path, scratch_database: str
) -> None:
    body = """\
bind = op.get_bind()
op.execute("CREATE TABLE seen (name text, value text)")
before = bind.exec_driver_sql("SHOW lock_timeout").scalar()
op.execute("SET LOCAL lock_timeout = 0")
after = bind.exec_driver_sql("SHOW lock_timeout").scalar()
op.execute(f"INSERT INTO seen VALUES ('before', '{before}'), ('after', '{after}')")
"""
    _upgrade(_chain(tmp_path / "chain", body), scratch_database)

    assert _seen(scratch_database) == {"before": "5s", "after": "0"}


def test_ddl_behind_a_long_transaction_fails_fast_and_names_the_setting(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_sql(scratch_database, "CREATE TABLE held (id int)")
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 300)
    cfg = _chain(tmp_path / "chain", 'op.execute("ALTER TABLE held ADD COLUMN x int")')
    holder = psycopg2.connect(scratch_database)
    try:
        with holder.cursor() as cur:
            # the server ends the holder if the timeout under test is missing:
            # the ALTER then succeeds and the test fails instead of hanging
            cur.execute("SET idle_in_transaction_session_timeout = 8000")
            cur.execute("LOCK TABLE held IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="MIGRATION_DDL_LOCK_TIMEOUT_MS=300"):
            _upgrade(cfg, scratch_database)
        waited = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    # it gave up on the lock, it did not wait for the holder (which never ends)
    assert waited < 10
    columns = run_sql(
        scratch_database,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'held'",
    )
    assert columns == [("id",)]


def test_a_statement_over_the_statement_timeout_names_the_setting(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "migration_statement_timeout_ms", 200)
    cfg = _chain(tmp_path / "chain", 'op.execute("SELECT pg_sleep(5)")')

    with pytest.raises(RuntimeError, match="MIGRATION_STATEMENT_TIMEOUT_MS=200"):
        _upgrade(cfg, scratch_database)


def test_any_other_database_error_is_not_rewritten(
    tmp_path: Path, scratch_database: str
) -> None:
    from sqlalchemy.exc import ProgrammingError

    cfg = _chain(
        tmp_path / "chain", 'op.execute("ALTER TABLE no_such_table ADD x int")'
    )

    with pytest.raises(ProgrammingError):
        _upgrade(cfg, scratch_database)


# The revisions the "Zero-downtime migrations" page includes: each one runs.

_SNIPPETS = Path(__file__).resolve().parents[4] / "docs" / "snippets" / "migrations"


def _snippet_chain(root: Path, name: str, version_table: str = "alembic_v") -> Config:
    return _chain_of_file(
        root, (_SNIPPETS / f"{name}.py").read_text(), version_table=version_table
    )


def _notes_table(url: str, rows: int = 3) -> None:
    run_sql(
        url,
        "CREATE TABLE notes (id serial PRIMARY KEY, title text NOT NULL, "
        "owner_id int, archived boolean)",
    )
    run_sql(
        url,
        "INSERT INTO notes (title, owner_id) "
        "SELECT 'n' || g, g FROM generate_series(1, %s) g",
        (rows,),
    )


def test_the_lifting_revision_waits_where_the_runner_would_give_up(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _notes_table(scratch_database)
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 300)
    holder = psycopg2.connect(scratch_database)
    with holder.cursor() as cur:
        cur.execute("LOCK TABLE notes IN ACCESS EXCLUSIVE MODE")
    # the same ALTER without the lift fails (the test above); with it, it waits
    release = threading.Timer(1.5, holder.rollback)
    release.start()
    try:
        _upgrade(
            _snippet_chain(tmp_path / "chain", "lift_lock_timeout"), scratch_database
        )
    finally:
        release.join()
        holder.close()

    columns = run_sql(
        scratch_database,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'notes' AND column_name = 'archived_at'",
    )
    assert columns == [("archived_at",)]


def test_the_concurrent_index_revision_builds_a_valid_index_and_drops_it(
    tmp_path: Path, scratch_database: str
) -> None:
    _notes_table(scratch_database)
    cfg = _snippet_chain(tmp_path / "chain", "concurrent_index")
    valid = (
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'ix_notes_owner_id'"
    )

    _upgrade(cfg, scratch_database)
    assert run_sql(scratch_database, valid) == [(True,)]

    command.downgrade(cfg, "base")
    assert run_sql(scratch_database, valid) == []


def test_the_backfill_revision_fills_every_row_over_several_batches(
    tmp_path: Path, scratch_database: str
) -> None:
    _notes_table(scratch_database, rows=12_000)  # three batches of 5000

    _upgrade(
        _snippet_chain(tmp_path / "chain", "backfill_in_batches"), scratch_database
    )

    assert run_sql(
        scratch_database, "SELECT count(*) FROM notes WHERE archived IS NULL"
    ) == [(0,)]


def test_the_two_step_constraint_revisions_add_it_not_valid_then_validate_it(
    tmp_path: Path, scratch_database: str
) -> None:
    _notes_table(scratch_database)
    state = (
        "SELECT convalidated FROM pg_constraint "
        "WHERE conname = 'ck_notes_title_not_empty'"
    )

    _upgrade(_snippet_chain(tmp_path / "add", "constraint_not_valid"), scratch_database)
    assert run_sql(scratch_database, state) == [(False,)]

    _upgrade(
        _snippet_chain(tmp_path / "validate", "constraint_validate", "alembic_w"),
        scratch_database,
    )
    assert run_sql(scratch_database, state) == [(True,)]


# What the runner's settings do inside autocommit_block() and to other errors.


def _autocommit(url: str, sql: str) -> None:
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def _index_validity(url: str) -> list[tuple[bool]]:
    return run_sql(
        url,
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'ix_notes_owner_id'",
    )


def test_the_settings_survive_autocommit_block(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 1234)
    monkeypatch.setattr(settings, "migration_statement_timeout_ms", 56_000)
    body = """\
bind = op.get_bind()
op.execute("CREATE TABLE seen (name text, value text)")
with op.get_context().autocommit_block():
    for name in ("lock_timeout", "statement_timeout"):
        value = bind.exec_driver_sql(f"SHOW {name}").scalar()
        op.execute(f"INSERT INTO seen VALUES ('{name}', '{value}')")
"""

    _upgrade(_chain(tmp_path / "chain", body), scratch_database)

    assert _seen(scratch_database) == {
        "lock_timeout": "1234ms",
        "statement_timeout": "56s",
    }


def test_the_concurrent_index_revision_outwaits_an_old_snapshot(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _notes_table(scratch_database)
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 300)
    holder = psycopg2.connect(scratch_database)
    with holder.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SELECT count(*) FROM notes")  # takes the snapshot
    # the build waits for that snapshot, longer than the runner's lock_timeout
    release = threading.Timer(1.5, holder.rollback)
    release.start()
    started = time.monotonic()
    try:
        _upgrade(
            _snippet_chain(tmp_path / "chain", "concurrent_index"), scratch_database
        )
        waited = time.monotonic() - started
    finally:
        release.join()
        holder.close()

    assert waited > 1.0
    assert _index_validity(scratch_database) == [(True,)]


def test_the_concurrent_index_revision_rebuilds_an_invalid_leftover(
    tmp_path: Path, scratch_database: str
) -> None:
    _notes_table(scratch_database)
    run_sql(scratch_database, "UPDATE notes SET owner_id = 1")
    # a failed concurrent build leaves an INVALID index under its name
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _autocommit(
            scratch_database,
            "CREATE UNIQUE INDEX CONCURRENTLY ix_notes_owner_id ON notes (owner_id)",
        )
    assert _index_validity(scratch_database) == [(False,)]

    _upgrade(_snippet_chain(tmp_path / "chain", "concurrent_index"), scratch_database)

    assert _index_validity(scratch_database) == [(True,)]


def test_a_failed_nowait_is_not_rewritten(
    tmp_path: Path, scratch_database: str
) -> None:
    run_sql(scratch_database, "CREATE TABLE held (id int)")
    cfg = _chain(
        tmp_path / "chain",
        'op.execute("LOCK TABLE held IN ACCESS EXCLUSIVE MODE NOWAIT")',
    )
    holder = psycopg2.connect(scratch_database)
    try:
        with holder.cursor() as cur:
            cur.execute("LOCK TABLE held IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(DBAPIError, match="could not obtain lock"):
            _upgrade(cfg, scratch_database)
    finally:
        holder.rollback()
        holder.close()


def test_a_lock_timeout_the_revision_set_itself_is_not_rewritten_at_zero(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_sql(scratch_database, "CREATE TABLE held (id int)")
    monkeypatch.setattr(settings, "migration_ddl_lock_timeout_ms", 0)
    body = """\
op.execute("SET LOCAL lock_timeout = 200")
op.execute("ALTER TABLE held ADD COLUMN x int")
"""
    holder = psycopg2.connect(scratch_database)
    try:
        with holder.cursor() as cur:
            cur.execute("SET idle_in_transaction_session_timeout = 8000")
            cur.execute("LOCK TABLE held IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(DBAPIError, match="lock timeout"):
            _upgrade(_chain(tmp_path / "chain", body), scratch_database)
    finally:
        holder.rollback()
        holder.close()


def test_a_cancelled_query_is_not_reported_as_a_statement_timeout(
    tmp_path: Path, scratch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "migration_statement_timeout_ms", 60_000)
    cfg = _chain(
        tmp_path / "chain",
        'op.execute("SELECT pg_cancel_backend(pg_backend_pid())")',
    )

    with pytest.raises(DBAPIError, match="user request"):
        _upgrade(cfg, scratch_database)
