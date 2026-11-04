"""The portable SQL forms of ``sandloom.sdk.data`` and their renderers.

On PostgreSQL each form renders what the standard modules wrote by hand before
(``citext``, ``postgresql_where``, a ``pg_trgm`` GIN index, ``ON CONFLICT``,
``pg_advisory_xact_lock``), so moving a module onto them needs no migration.
No form falls back silently: without a renderer it refuses.
"""

from __future__ import annotations

import importlib.metadata
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

import sandloom.core.app_factory as app_factory
import sandloom.core.sql_alchemy.portable as portable
from sandloom.core.app_factory import sql_dialect_problem
from sandloom.core.drivers.sql_dialects import PostgresqlSql
from sandloom.core.migrations.alembic_support import _created_on, _render_item
from sandloom.core.sql_alchemy.portable import UnsupportedSqlForm, register_sql_dialect
from sandloom.sdk.data import (
    CaseInsensitiveText,
    conflict_insert,
    partial_index,
    search_index,
    transaction_lock,
)

PG = postgresql.dialect()  # type: ignore[no-untyped-call]
LITE = sqlite.dialect()
MYSQL = mysql.dialect()


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """The renderer registry as it was before the test, afterwards."""
    registered = dict(portable._registered)
    state = dict(portable._state)
    yield
    portable._registered.clear()
    portable._registered.update(registered)
    portable._state.update(state)


@pytest.fixture
def no_renderers(isolated_registry: None) -> None:
    """An environment with no renderer at all (no entry points installed)."""
    portable._registered.clear()
    portable._state["loaded"] = True


def _table(*items: Any) -> sa.Table:
    return sa.Table(
        "people",
        sa.MetaData(),
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("email", CaseInsensitiveText, nullable=False),
        sa.Column("name", sa.String(50)),
        sa.Column("status", sa.String(16)),
        *items,
    )


def _ddl(element: Any, dialect: Any) -> str:
    return " ".join(str(element.compile(dialect=dialect)).split())


class _Session:
    """The calls the forms make on an AsyncSession."""

    def __init__(self, dialect: Any) -> None:
        self._bind = type("Bind", (), {"dialect": dialect})()
        self.statements: list[str] = []

    def get_bind(self) -> Any:
        return self._bind

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement.compile(dialect=self._bind.dialect)))


# --------------------------------------------------------------- column type


def test_case_insensitive_text_is_citext_on_postgres_and_nocase_on_sqlite() -> None:
    table = _table()
    assert "email CITEXT NOT NULL" in _ddl(CreateTable(table), PG)
    assert 'email VARCHAR COLLATE "NOCASE" NOT NULL' in _ddl(CreateTable(table), LITE)


def test_case_insensitive_text_refuses_a_database_without_a_renderer() -> None:
    with pytest.raises(UnsupportedSqlForm, match="CaseInsensitiveText.*'mysql'"):
        _ddl(CreateTable(_table()), MYSQL)


def test_case_insensitive_text_length_is_part_of_its_cache_key() -> None:
    assert (
        CaseInsensitiveText(80)._static_cache_key
        != CaseInsensitiveText(20)._static_cache_key
    )
    assert "VARCHAR(80)" in _ddl(
        sa.cast(sa.literal("a"), CaseInsensitiveText(80)), LITE
    )


# --------------------------------------------------------------- partial index


@pytest.mark.parametrize("dialect", [PG, LITE], ids=["postgresql", "sqlite"])
def test_partial_index_has_its_condition(dialect: Any) -> None:
    index = partial_index("uq_pending", "email", where="status = 'p'", unique=True)
    _table(index)
    assert _ddl(CreateIndex(index), dialect) == (
        "CREATE UNIQUE INDEX uq_pending ON people (email) WHERE status = 'p'"
    )


def test_partial_index_without_a_renderer_refuses_instead_of_indexing_every_row(
    no_renderers: None,
) -> None:
    index = partial_index("uq_pending", "email", where="status = 'p'", unique=True)
    sa.Table("t", sa.MetaData(), sa.Column("email", sa.String(9)), index)
    with pytest.raises(UnsupportedSqlForm, match="partial_index.*'postgresql'"):
        CreateIndex(index).compile(dialect=PG)


def test_a_renderer_registered_later_reaches_an_existing_index(
    no_renderers: None,
) -> None:
    index = partial_index("uq_pending", "email", where="status = 'p'")
    sa.Table("t", sa.MetaData(), sa.Column("email", sa.String(9)), index)
    register_sql_dialect("postgresql", PostgresqlSql())
    assert _ddl(CreateIndex(index), PG).endswith("WHERE status = 'p'")


# ---------------------------------------------------------------- search index


def test_search_index_is_the_trigram_gin_index_on_postgres() -> None:
    index = search_index("ix_search", "name", "email", case_insensitive=["email"])
    _table(index)
    assert _ddl(CreateIndex(index), PG) == (
        "CREATE INDEX ix_search ON people USING gin "
        "(name gin_trgm_ops, (email::text) gin_trgm_ops)"
    )


def test_search_index_is_not_created_where_the_database_has_none() -> None:
    index = search_index("ix_search", "name")
    table = _table(index)
    engine = sa.create_engine("sqlite://")
    table.metadata.create_all(engine)
    assert sa.inspect(engine).get_indexes("people") == []
    assert _created_on(index, LITE) is False
    assert _created_on(index, PG) is True
    with pytest.raises(UnsupportedSqlForm, match="search_index"):
        CreateIndex(index).compile(dialect=LITE)


def test_search_index_refuses_a_case_insensitive_column_it_does_not_index() -> None:
    with pytest.raises(ValueError, match="email"):
        search_index("ix", "name", case_insensitive=["email"])


# ---------------------------------------------------------------- statements


@pytest.mark.parametrize("dialect", [PG, LITE], ids=["postgresql", "sqlite"])
def test_conflict_insert_renders_on_conflict(dialect: Any) -> None:
    table = _table()
    session: Any = _Session(dialect)
    ignore = (
        conflict_insert(table, session=session)
        .values(email="a")
        .on_conflict_do_nothing(index_elements=["email"])
    )
    assert "ON CONFLICT (email) DO NOTHING" in _ddl(ignore, dialect)
    upsert = conflict_insert(table, session=session).values(email="a", name="n")
    upsert = upsert.on_conflict_do_update(
        index_elements=[table.c.email], set_={"name": upsert.excluded.name}
    )
    assert "ON CONFLICT (email) DO UPDATE SET name = excluded.name" in _ddl(
        upsert, dialect
    )


def test_conflict_insert_refuses_a_database_without_a_renderer() -> None:
    session: Any = _Session(MYSQL)
    with pytest.raises(UnsupportedSqlForm, match="conflict_insert.*no SQL dialect"):
        conflict_insert(_table(), session=session)


async def test_transaction_lock_takes_an_advisory_lock_on_postgres() -> None:
    session: Any = _Session(PG)
    await transaction_lock(42, session=session)
    assert len(session.statements) == 1
    assert session.statements[0].startswith("SELECT pg_advisory_xact_lock(")


@pytest.mark.parametrize("dialect", [LITE, MYSQL], ids=["sqlite", "mysql"])
async def test_transaction_lock_refuses_rather_than_run_unlocked(dialect: Any) -> None:
    session: Any = _Session(dialect)
    with pytest.raises(UnsupportedSqlForm, match="transaction_lock"):
        await transaction_lock(42, session=session)
    assert session.statements == []


# ---------------------------------------------------------------- the registry


def test_a_driver_package_adds_a_database_by_registering_a_renderer(
    isolated_registry: None,
) -> None:
    class Renamed(postgresql.base.PGDialect):
        name = "plugin_pg"

    register_sql_dialect("plugin_pg", PostgresqlSql())
    dialect = Renamed()  # type: ignore[no-untyped-call]
    assert "email CITEXT NOT NULL" in _ddl(CreateTable(_table()), dialect)


def test_a_renderer_that_uses_a_form_on_import_does_not_hang(
    isolated_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading a renderer must not wait on itself (deadlock or recursion)."""

    class Entry:
        name = "postgresql"
        value = "plugin:RENDERER"

        def load(self) -> Any:
            partial_index("ix_on_import", "a", where="a > 1")
            register_sql_dialect("other", PostgresqlSql())
            return PostgresqlSql()

    class EntryPoints:
        def select(self, group: str) -> list[Entry]:
            return [Entry()]

    monkeypatch.setattr(importlib.metadata, "entry_points", EntryPoints)
    portable._registered.clear()
    portable._state["loaded"] = False
    done = threading.Event()

    def load() -> None:
        portable._renderers()
        done.set()

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert done.is_set(), "loading the renderers hung"
    assert set(portable._registered) == {"postgresql", "other"}


# ----------------------------------------------------- migrations and startup


def test_a_migration_writes_the_database_type_and_imports_it() -> None:
    imports: set[str] = set()
    context = type("Ctx", (), {"dialect": PG, "imports": imports})()
    assert _render_item("type", CaseInsensitiveText(), context) == "postgresql.CITEXT()"
    assert imports == {"from sqlalchemy.dialects import postgresql"}
    assert _render_item("type", sa.String(5), context) is False


def test_startup_refuses_a_database_without_a_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def infra(url: str) -> Any:
        instance = type("Instance", (), {"options": {"url": url}})()
        return lambda _cfg: {"database": {"default": instance}}

    monkeypatch.setattr(app_factory, "load_infra_config", infra("mysql+aiomysql://h/d"))
    problem = sql_dialect_problem(object())
    assert problem is not None
    assert "default (mysql)" in problem
    assert "SANDLOOM-B024" in problem
    monkeypatch.setattr(
        app_factory, "load_infra_config", infra("postgresql+asyncpg://h/d")
    )
    assert sql_dialect_problem(object()) is None
