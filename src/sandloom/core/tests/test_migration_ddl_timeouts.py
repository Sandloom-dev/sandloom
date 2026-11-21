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
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

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


def _chain(root: Path, upgrade_body: str) -> Config:
    """A chain of one revision whose ``upgrade()`` runs ``upgrade_body``."""
    (root / "versions").mkdir(parents=True)
    (root / "env.py").write_text(_ENV)
    (root / "versions" / "0001_probe.py").write_text(
        "from alembic import op\n"
        "revision = '0001'\n"
        "down_revision = None\n\n\n"
        "def upgrade() -> None:\n"
        + textwrap.indent(textwrap.dedent(upgrade_body), "    ")
        + "\n\n\ndef downgrade() -> None:\n    pass\n"
    )
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    cfg.set_main_option("version_table", "alembic_version_ddltimeouts")
    return cfg


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
