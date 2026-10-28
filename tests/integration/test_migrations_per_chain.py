"""Per-(module|core) migration chains: structure, ownership, and DB parity.

The framework no longer uses a single linear chain. Each business module owns
its own Alembic chain (``src/sandloom/<module>/migrations``) covering only its
tables, and the core chain owns the shared infra tables (outbox/audit/inbox).
These tests assert:

* **ownership** — every ORM table is created by exactly one chain, the chains
  are disjoint, together they cover the tables of the core and every loaded
  module, and each table by the chain of the package declaring its model
  (static, no DB needed). The chains come from the loaded descriptors: a new
  module is covered without editing this file;
* **parity** — applying every chain to a fresh database reproduces the ORM
  schema exactly (tables + columns + declared indexes), proving the hand-shipped
  baselines never drift from the models the app actually uses (needs Postgres).
"""

from __future__ import annotations

import pytest

from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.orm import Base
from sandloom.core.testing import (
    MigrationChain,
    migration_chains,
    model_tables,
    tables_created_by,
)

CORE_TABLES = {
    "outbox_messages",
    "audit_log",
    "consumer_inbox",
    "contract_command_log",
    "http_idempotency_log",
    "saga_instances",
    "saga_steps",
    "projection_watermarks",
}


def _chains() -> list[MigrationChain]:
    """Every chain of the loaded modules (derived, never a hand-kept list)."""
    return migration_chains()


def test_migrations_cli_loads_modules_with_chains() -> None:
    """`sandloom-migrate` must load modules whose register() registers event schemas.

    The CLI builds a bare facade; it has to wire a SchemaRegistry (as create_app
    does) or every module's register() raises and `upgrade-all` exits 1 before
    running a single migration. Regression for that container-only failure.
    """
    from sandloom.core.cli.migrate import _load_core
    from sandloom.core.descriptors.module_names import installed_modules

    DEFAULT_MODULES = [m.path for m in installed_modules()]

    core = _load_core()
    by_name = {d.name: d for d in core.module_descriptors}
    for name in DEFAULT_MODULES:
        assert name in by_name, f"{name} failed to load in the migrations CLI"
        assert by_name[name].migrations_path is not None, (
            f"{name} chain not auto-detected"
        )


def test_every_chain_has_a_baseline() -> None:
    for chain in _chains():
        revisions = list(chain.versions.glob("*.py"))
        assert revisions, f"{chain.key} chain has no migration revisions"


def test_core_chain_owns_only_infra_tables() -> None:
    core = next(c for c in _chains() if c.key == "core")
    assert tables_created_by(core) == CORE_TABLES


def test_chains_are_disjoint_and_cover_all_orm_tables() -> None:
    per_chain = {chain.key: tables_created_by(chain) for chain in _chains()}

    # No table is created by two chains.
    seen: dict[str, str] = {}
    for label, tables in per_chain.items():
        for t in tables:
            assert t not in seen, f"{t} created by both {seen[t]} and {label}"
            seen[t] = label

    # Together the chains build exactly the ORM schema of the core and the
    # loaded modules — nothing missing, no orphan table from an old chain —
    # and each table by the chain of the package that declares its model.
    owners = model_tables()
    assert set(seen) == set(owners), (
        f"missing={set(owners) - set(seen)} extra={set(seen) - set(owners)}"
    )
    wrong = {t: (seen[t], c.key) for t, c in owners.items() if seen[t] != c.key}
    assert not wrong, f"table created by another chain than its owner's: {wrong}"


# --- DB-level parity (needs a reachable Postgres) ----------------------------

_DEFAULT_SYNC_URL = (
    "postgresql+psycopg2://test_user:test_password@localhost:5433/sandloom_test"
)


def _sync_url() -> str:
    import os

    raw = os.getenv("DATABASE_URL") or _DEFAULT_SYNC_URL
    # Force IPv4: ``localhost`` may resolve to ``::1`` where the container is not
    # bound, while the published port is on 127.0.0.1.
    return to_sync_url(raw).replace("@localhost:", "@127.0.0.1:")


@pytest.fixture()
def _scratch_db() -> str:
    """Create an isolated throwaway database, yield its sync URL, drop it after.

    Running the real chains is destructive (it builds a whole schema), so it
    must not share the suite's ``sandloom_test`` database. The configured database is
    used as the maintenance connection (the ``postgres`` DB is not necessarily
    reachable by the test role); the scratch DB is cloned from ``template0`` to
    sidestep any host collation-version mismatch on ``template1``.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base_url = make_url(_sync_url())
    scratch_name = f"{base_url.database}_perchain"
    # ``str(URL)`` masks the password as ``***`` in SQLAlchemy 2.0 — render it
    # explicitly so the engine receives the real credentials.
    admin_dsn = base_url.render_as_string(hide_password=False)
    scratch_dsn = base_url.set(database=scratch_name).render_as_string(
        hide_password=False
    )

    def _admin() -> object:
        return create_engine(admin_dsn, isolation_level="AUTOCOMMIT")

    admin = _admin()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}"'))
            conn.execute(text(f'CREATE DATABASE "{scratch_name}" TEMPLATE template0'))
    except Exception as exc:  # pragma: no cover  # noqa: BLE001 - test Postgres unreachable: skip
        admin.dispose()
        pytest.skip(f"Postgres not reachable for parity test: {exc}")
    finally:
        admin.dispose()

    yield scratch_dsn

    admin = _admin()
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": scratch_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}"'))
    finally:
        admin.dispose()


def test_chains_reproduce_orm_schema_exactly(_scratch_db: str) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    for chain in _chains():
        cfg = Config()
        cfg.set_main_option("script_location", str(chain.script_location))
        cfg.set_main_option("sqlalchemy.url", _scratch_db)
        cfg.set_main_option("version_table", chain.version_table)
        command.upgrade(cfg, "head")

    engine = create_engine(_scratch_db)
    try:
        insp = inspect(engine)
        db_tables = {
            t for t in insp.get_table_names() if not t.startswith("alembic_version")
        }
        orm_tables = set(model_tables())
        assert db_tables == orm_tables, (
            f"missing={orm_tables - db_tables} extra={db_tables - orm_tables}"
        )

        for table in sorted(orm_tables):
            orm_cols = set(Base.metadata.tables[table].columns.keys())
            db_columns = {c["name"]: c for c in insp.get_columns(table)}
            assert orm_cols == set(db_columns), f"column drift on {table}"
            # A CreatedAt / UpdatedAt column defaults to clock_timestamp() in
            # the migrated database too (autogenerate compares no defaults).
            for column in Base.metadata.tables[table].columns:
                if "clock_timestamp" in str(getattr(column.server_default, "arg", "")):
                    assert db_columns[column.name]["default"] == "clock_timestamp()", (
                        f"{table}.{column.name}: the chain's default is "
                        f"{db_columns[column.name]['default']!r}"
                    )
            orm_idx = {i.name for i in Base.metadata.tables[table].indexes}
            db_idx = {i["name"] for i in insp.get_indexes(table)}
            assert orm_idx <= db_idx, f"missing indexes on {table}: {orm_idx - db_idx}"
            # ...and no stale index the model no longer declares (a dropped
            # duplicate must be dropped by a migration too). Indexes backing a
            # UNIQUE constraint are the constraint's, not the model's indexes.
            stale = {
                i["name"]
                for i in insp.get_indexes(table)
                if "duplicates_constraint" not in i
            } - orm_idx
            assert not stale, f"stale indexes on {table}: {stale}"
    finally:
        engine.dispose()
