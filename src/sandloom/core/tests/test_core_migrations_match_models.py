"""Migrations match models (risk fix 4): the core chain at head builds the
schema the kernel's models declare, and the comparison bites.

PostgreSQL only: SQLite takes its schema from the models (SANDLOOM-B028).
"""

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from sandloom.core.migrations.alembic_support import COMPARE_OPTIONS
from sandloom.core.migrations.schema_compare import (
    _index_definition_drift,
    schema_differences,
)
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.testing.migrations import (
    _created_tables,
    _find_chain,
    _owned_tables,
    assert_migrations_match_models,
    migration_chains,
    migration_drift,
    run_sql,
    runs_no_chains,
    temporary_database,
    upgrade_core_chain,
)

needs_postgresql = pytest.mark.skipif(
    runs_no_chains(os.environ.get("DATABASE_URL", "")),
    reason="needs PostgreSQL: SQLite runs no chain (SANDLOOM-B028)",
)


def test_core_chain_matches_its_models() -> None:
    assert_migrations_match_models("core")


@needs_postgresql
def test_the_comparison_names_each_difference() -> None:
    """A database the chain built, then changed by hand: each change is one
    line naming the table and the object."""
    with temporary_database() as url, temporary_database() as models_url:
        upgrade_core_chain(url)
        _tamper(url)
        chain = _find_chain(migration_chains(), "core")
        owned = _owned_tables(chain, None)
        drift = "\n".join(schema_differences(url, owned, models_url))
    assert "column outbox_messages.trace_id (VARCHAR(64)): in the models" in drift
    assert "index ix_audit_log_bite on audit_log: created by the chain" in drift
    assert (
        "column projection_watermarks.rebuilds_active: server default differs "
        "(database '7', models '0')"
    ) in drift
    saga = next(line for line in drift.splitlines() if "ix_saga_instances_due" in line)
    assert saga.startswith(
        "index ix_saga_instances_due on saga_instances: definition differs: "
        "dialect_options (database {"
    )
    assert "'postgresql_using': 'hash'" in saga
    assert "(status)::text = 'running'::text" in saga


def _tamper(url: str) -> None:
    for statement in (
        # a model column the chain never added
        "ALTER TABLE outbox_messages DROP COLUMN trace_id",
        # an index only the database has
        "CREATE INDEX ix_audit_log_bite ON audit_log (actor_type)",
        # a server default the model does not declare
        "ALTER TABLE projection_watermarks ALTER COLUMN rebuilds_active SET DEFAULT 7",
        # same name, columns and uniqueness; another access method and condition
        "DROP INDEX ix_saga_instances_due",
        "CREATE INDEX ix_saga_instances_due ON saga_instances USING hash "
        "(next_attempt_at) WHERE status = 'running'",
    ):
        run_sql(url, statement)


# --------------------------------------------------------------------------- #
# Index definitions: the model's index against the DDL a revision would run
# --------------------------------------------------------------------------- #

Model = Callable[[sa.Table], sa.Index]


def _table(metadata: sa.MetaData) -> sa.Table:
    return sa.Table(
        "probe",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("a", sa.String(20)),
        sa.Column("b", sa.Integer),
        sa.Column("c", sa.Text),
        sa.Column("s", sa.String(10)),
        sa.Column("f", sa.Boolean),
    )


_SAME: list[tuple[str, Model, str]] = [
    ("desc", lambda t: sa.Index("ix", t.c.a.desc()), "(a DESC)"),
    (
        "include",
        lambda t: sa.Index("ix", t.c.a, postgresql_include=["b"]),
        "(a) INCLUDE (b)",
    ),
    (
        "not in",
        lambda t: sa.Index("ix", t.c.a, postgresql_where=t.c.s.notin_(["x", "y"])),
        "(a) WHERE s NOT IN ('x', 'y')",
    ),
    (
        "in",
        lambda t: sa.Index("ix", t.c.a, postgresql_where=t.c.s.in_(["x", "y"])),
        "(a) WHERE s IN ('x', 'y')",
    ),
    (
        "opclass",
        lambda t: sa.Index(
            "ix", t.c.c, postgresql_using="gin", postgresql_ops={"c": "gin_trgm_ops"}
        ),
        "USING gin (c gin_trgm_ops)",
    ),
    (
        "expression opclass",
        lambda t: sa.Index(
            "ix", sa.text("(c::text) gin_trgm_ops"), postgresql_using="gin", _table=t
        ),
        "USING gin ((c::text) gin_trgm_ops)",
    ),
]

_DIFFERENT: list[tuple[str, Model, str]] = [
    ("desc only in the database", lambda t: sa.Index("ix", t.c.a), "(a DESC)"),
    (
        "nulls first only in the database",
        lambda t: sa.Index("ix", t.c.a),
        "(a NULLS FIRST)",
    ),
    (
        "include only in the database",
        lambda t: sa.Index("ix", t.c.a),
        "(a) INCLUDE (b)",
    ),
    (
        "literal case",
        lambda t: sa.Index("ix", t.c.a, postgresql_where=t.c.s == "X"),
        "(a) WHERE s = 'x'",
    ),
    (
        "regrouped booleans",
        lambda t: sa.Index(
            "ix", t.c.a, postgresql_where=sa.and_(sa.or_(t.c.b == 1, t.c.b == 2), t.c.f)
        ),
        "(a) WHERE b = 1 OR (b = 2 AND f)",
    ),
    (
        "another operator class",
        lambda t: sa.Index(
            "ix", t.c.c, postgresql_using="gin", postgresql_ops={"c": "gin_trgm_ops"}
        ),
        "USING gist (c gist_trgm_ops)",
    ),
]


@pytest.fixture(scope="module")
def two_databases() -> Iterator[tuple[sa.Engine, sa.Engine]]:
    if runs_no_chains(os.environ.get("DATABASE_URL", "")):
        pytest.skip("needs PostgreSQL: SQLite runs no chain (SANDLOOM-B028)")
    with temporary_database() as chain_url, temporary_database() as models_url:
        engines = tuple(
            sa.create_engine(to_sync_url(u), poolclass=sa.pool.NullPool)
            for u in (chain_url, models_url)
        )
        for engine in engines:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        yield engines[0], engines[1]


def _index_drift(
    engines: tuple[sa.Engine, sa.Engine],
    model: Model,
    ddl: str,
    create: str = "CREATE INDEX",
) -> list[str]:
    chain, models = engines
    metadata = sa.MetaData()
    table = _table(metadata)
    index = model(table)
    with chain.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS probe")
        table.indexes.discard(index)
        table.create(connection)  # what the revision's create_table wrote
        connection.exec_driver_sql(f"{create} ix ON probe {ddl}")
        table.indexes.add(index)
    with models.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS probe")
        table.create(connection)  # the model, index included
    with chain.connect() as c, models.connect() as m:
        return _index_definition_drift(c, m, frozenset({"probe"}))


@pytest.mark.parametrize(("label", "model", "ddl"), _SAME, ids=[c[0] for c in _SAME])
def test_an_index_written_another_way_matches(
    two_databases: Any, label: str, model: Model, ddl: str
) -> None:
    assert _index_drift(two_databases, model, ddl) == [], label


@pytest.mark.parametrize(
    ("label", "model", "ddl"), _DIFFERENT, ids=[c[0] for c in _DIFFERENT]
)
def test_an_index_defined_differently_is_named(
    two_databases: Any, label: str, model: Model, ddl: str
) -> None:
    drift = _index_drift(two_databases, model, ddl)
    assert len(drift) == 1 and drift[0].startswith(
        "index ix on probe: definition differs: "
    ), (label, drift)


def test_nulls_not_distinct_on_both_sides_matches(two_databases: Any) -> None:
    chain, _ = two_databases
    with chain.connect() as connection:
        if connection.dialect.server_version_info < (15,):
            pytest.skip("NULLS NOT DISTINCT needs PostgreSQL 15")

    def model(t: sa.Table) -> sa.Index:
        return sa.Index("ix", t.c.a, unique=True, postgresql_nulls_not_distinct=True)

    drift = _index_drift(
        two_databases, model, "(a) NULLS NOT DISTINCT", create="CREATE UNIQUE INDEX"
    )
    assert drift == []
    assert _index_drift(two_databases, model, "(a)", create="CREATE UNIQUE INDEX")


# --------------------------------------------------------------------------- #
# Server defaults of a JSON column (json has no equality operator)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("database", "model", "differs"),
    [
        ("'{}'::json", "{}", False),
        ("'{ }'", "{}", False),
        ("'{\"a\": 1}'::json", '{"a":1}', False),
        ("'{\"a\": 1}'::json", "{}", True),
        ("'[]'::jsonb", "{}", True),
    ],
)
def test_a_json_default_is_compared_without_sql(
    two_databases: Any, database: str, model: str, differs: bool
) -> None:
    chain, _ = two_databases
    with chain.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS jsonprobe")
        column = "jsonb" if "jsonb" in database else "json"
        connection.exec_driver_sql(
            f"CREATE TABLE jsonprobe (id integer PRIMARY KEY, "
            f"meta {column} DEFAULT {database})"
        )
    metadata = sa.MetaData()
    json_type = (
        sa.JSON()
        if column == "json"
        else sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    )
    sa.Table(
        "jsonprobe",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("meta", json_type, server_default=model),
    )
    with chain.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                **COMPARE_OPTIONS,
                "include_object": lambda o, n, t, r, c: (
                    t != "table" or n == "jsonprobe"
                ),
            },
        )
        diffs = compare_metadata(context, metadata)
    defaults = [d for d in diffs if isinstance(d, list)]
    assert bool(defaults) is differs, diffs


# --------------------------------------------------------------------------- #
# Helper edges
# --------------------------------------------------------------------------- #


def test_an_unknown_module_is_named() -> None:
    with pytest.raises(ValueError, match="no migration chain for 'nope'"):
        _find_chain(migration_chains(), "nope")


def test_without_a_database_url_the_reason_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="needs PostgreSQL"):
        migration_drift("core")


def test_a_table_a_downgrade_recreates_is_not_owned() -> None:
    source = (
        "def upgrade():\n    op.create_table('kept', sa.Column('id'))\n"
        "    op.drop_table('moved')\n"
        "def downgrade():\n    op.create_table('moved', sa.Column('id'))\n"
    )
    assert _created_tables(source, upgrade_only=True) == {"kept"}
    assert _created_tables(source) == {"kept", "moved"}
