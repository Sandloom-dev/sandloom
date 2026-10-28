"""Fast per-test database reset for module test suites.

Recreating the schema (``drop_all`` + ``create_all`` over every table in
``Base.metadata``) before each test costs ~0.8s. :func:`reset_schema` creates
the schema once per process and database; later calls empty it again.

``TRUNCATE`` gives every table a new file (plus its indexes and TOAST), so
truncating ALL tables costs ~0.1 s on an idle server and several times that
under load — paid by every test. A test touches a few tables, so the reset
first asks which tables hold a row (one capped ``SELECT`` per table in a
single statement, with ``row_security = off`` so a row-level-security policy
can never hide a row — the statement errors instead and the reset falls back
to truncating everything), ``DELETE``s those children-first (a table holding
many rows is truncated instead) and rewinds every used sequence (a
rolled-back insert still consumes a value). The result is the same as
truncating every table: all of them empty, every owned sequence at its
start.

If another test dropped or altered a table in the meantime (core outbox
fixtures, migration tests), the truncate fails and the schema is rebuilt from
scratch, so the fast path never leaves a test on a broken schema.

The module also holds the relational half of the ``sandloom.core.testing`` pytest
plugin (:func:`open_test_engine` / :func:`open_test_session`), so the plugin
itself stays free of SQLAlchemy imports.

Test-only: never call this from application code.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import (
    MetaData,
    UniqueConstraint,
    delete,
    func,
    literal,
    select,
    text,
    union_all,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import sandloom.core.sql_alchemy.models
import sandloom.core.sql_alchemy.models.audit  # noqa: F401
from sandloom.core._internal.db_provider import (
    build_disposable_engine,
)
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.testing.connections import (
    TEST_ENGINE_MAX_OVERFLOW,
    TEST_ENGINE_POOL_SIZE,
)
from sandloom.core.testing.ownership import claim_database

logger = logging.getLogger(__name__)

_EXTENSIONS = ("uuid-ossp", "citext", "pg_trgm")

# (database url, metadata identity, table set) the schema was last built for
# in this process. A new table registered since then forces a rebuild.
_built: set[tuple[str, int, frozenset[str]]] = set()
# The sequences owned by the tables of a built schema (rewound on reset).
_sequences: dict[tuple[str, int, frozenset[str]], list[str]] = {}

_OWNED_SEQUENCES = """
SELECT quote_ident(sn.nspname) || '.' || quote_ident(s.relname)
  FROM pg_class s
  JOIN pg_namespace sn ON sn.oid = s.relnamespace
  JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass
                  AND d.deptype IN ('a', 'i')
 WHERE s.relkind = 'S' AND d.refobjid = ANY(CAST(:tables AS regclass[]))
"""


def _key(engine: AsyncEngine, metadata: MetaData) -> tuple[str, int, frozenset[str]]:
    url = engine.url.render_as_string(hide_password=False)
    return (url, id(metadata), frozenset(metadata.tables))


async def _rebuild(engine: AsyncEngine, metadata: MetaData) -> None:
    async with engine.begin() as conn:
        for ext in _EXTENSIONS:
            await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext}"'))
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)


def _qualified(engine: AsyncEngine, metadata: MetaData) -> list[str]:
    fmt = engine.dialect.identifier_preparer.format_table
    return [fmt(t) for t in metadata.sorted_tables]


#: A dirty table with more rows than this is TRUNCATEd instead of DELETEd.
DELETE_ROW_LIMIT = 1000


async def _truncate_dirty(
    engine: AsyncEngine, metadata: MetaData, sequences: list[str]
) -> None:
    """Empty the tables that hold a row; rewind every used sequence.

    ``metadata.sorted_tables`` is in dependency order (parents first).
    Small dirty tables are emptied with ``DELETE`` children-first (no new
    files, unlike ``TRUNCATE``, whose ``CASCADE`` also rewrites every
    referencing table); a table holding more than :data:`DELETE_ROW_LIMIT`
    rows is truncated. Any error (a row-security policy, a foreign key from
    outside the metadata) propagates and the caller truncates everything.
    """
    sorted_tables = metadata.sorted_tables
    tables = _qualified(engine, metadata)
    probe = union_all(
        *(
            select(literal(n), func.count())
            .select_from(
                select(literal(1)).select_from(t).limit(DELETE_ROW_LIMIT + 1).subquery()
            )
            .having(func.count() > 0)
            for n, t in enumerate(sorted_tables)
        )
    )
    async with engine.begin() as conn:
        # Never let a row-level-security policy hide a row from the probe (or
        # spare one from the DELETE): with row_security off, a statement RLS
        # would filter fails instead.
        await conn.execute(text("SET LOCAL row_security = off"))
        rows = {int(n): int(c) for n, c in await conn.execute(probe)}
        big = sorted(n for n, c in rows.items() if c > DELETE_ROW_LIMIT)
        if big:
            await conn.execute(
                text(
                    f"TRUNCATE {', '.join(tables[n] for n in big)} "
                    "RESTART IDENTITY CASCADE"
                )
            )
        for n in sorted(set(rows) - set(big), reverse=True):  # children first
            await conn.execute(delete(sorted_tables[n]))
        if sequences:
            # DELETE keeps sequences where they are, and a rolled-back insert
            # into a table that is empty now advanced one too: rewind every
            # used one to its start (what RESTART IDENTITY does).
            await conn.execute(
                text(
                    "SELECT setval(CAST(format('%I.%I', schemaname, sequencename)"
                    " AS regclass), start_value, false) FROM pg_sequences"
                    " WHERE last_value IS NOT NULL AND"
                    " CAST(format('%I.%I', schemaname, sequencename) AS regclass)"
                    " = ANY(CAST(:seqs AS regclass[]))"
                ),
                {"seqs": sequences},
            )


async def _truncate_all(engine: AsyncEngine, tables: list[str]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        )


async def reset_schema(engine: AsyncEngine, metadata: MetaData) -> None:
    """Leave every table of ``metadata`` present and empty, sequences rewound."""
    key = _key(engine, metadata)
    if key in _built and metadata.sorted_tables:
        tables = _qualified(engine, metadata)
        try:
            await _truncate_dirty(engine, metadata, _sequences.get(key, []))
            return
        except DBAPIError:
            logger.debug("dirty-table reset failed; truncating all", exc_info=True)
        try:
            await _truncate_all(engine, tables)
            return
        except DBAPIError:
            logger.debug("truncate failed; rebuilding test schema", exc_info=True)
            _built.discard(key)
    await _rebuild(engine, metadata)
    _built.add(key)
    _sequences[key] = await _owned_sequences(engine, metadata)


async def _owned_sequences(engine: AsyncEngine, metadata: MetaData) -> list[str]:
    tables = _qualified(engine, metadata)
    if not tables:
        return []
    async with engine.connect() as conn:
        rows = await conn.execute(text(_OWNED_SEQUENCES), {"tables": tables})
        return [str(name) for (name,) in rows]


def assert_disposable_test_database(database_url: str) -> None:
    """Refuse to reset anything but an obviously disposable TEST database.

    The test harness empties every table on ``database_url``; a shell that still
    exports a dev/prod ``DATABASE_URL`` must never get that far. A database
    name containing ``test`` is required even when ``ENVIRONMENT=test``.
    """
    db_name = (make_url(database_url).database or "").lower()
    if "test" not in db_name:
        raise RuntimeError(
            f"Refusing to reset tables on database {db_name!r}: test fixtures "
            "only run against a database whose name contains 'test'. "
            "ENVIRONMENT=test does not authorize destructive setup. "
            "Check DATABASE_URL in .env.test."
        )
    # A run that names its own database (SANDLOOM_TEST_DATABASE_URL) must never
    # reset another one, e.g. a shared database a stale .env.test points at.
    intended = os.getenv("SANDLOOM_TEST_DATABASE_URL")
    if intended:
        want = (make_url(intended).database or "").lower()
        if db_name != want and not db_name.startswith(f"{want}_gw"):
            raise RuntimeError(
                f"Refusing to reset tables on database {db_name!r}: this run "
                f"set SANDLOOM_TEST_DATABASE_URL to database {want!r}."
            )


@asynccontextmanager
async def open_test_engine(
    database_url: str | None = None, *, metadata: MetaData | None = None
) -> AsyncIterator[AsyncEngine]:
    """A caller-owned engine on the TEST database with a fresh, empty schema.

    ``database_url`` defaults to ``DATABASE_URL`` from the environment (the
    ``.env.test`` the plugin loaded). Build the application BEFORE calling this
    so every loaded module's models are registered on ``metadata`` (default:
    the shared ``Base.metadata``).
    """
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set: add it to .env.test (the sandloom.core.testing "
            "plugin loads it) or export it"
        )
    assert_disposable_test_database(url)
    # Never reset a database another test run owns (marker on the database;
    # claimed when unmarked) — sandloom.core.testing.ownership.
    claim_database(url)
    if metadata is None:
        # The shared core-infra tables (outbox / audit / consumer inbox) are
        # always part of the test schema: bind_test_uow writes outbox + audit
        # rows, durable consumers claim inbox rows.
        metadata = Base.metadata

    engine = build_disposable_engine(
        url, pool_size=TEST_ENGINE_POOL_SIZE, max_overflow=TEST_ENGINE_MAX_OVERFLOW
    )
    try:
        await reset_schema(engine, metadata)
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def open_test_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on ``engine`` that is rolled back when the test ends."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


def _plain_columns(index: Any) -> tuple[str, ...] | None:
    """Column names of a plain btree index (None for expression/GIN/partial)."""
    if index.expressions and any(not hasattr(e, "name") for e in index.expressions):
        return None
    kw = index.dialect_kwargs
    if kw.get("postgresql_using") or kw.get("postgresql_where") is not None:
        return None
    return tuple(c.name for c in index.columns)


def redundant_indexes(metadata: MetaData) -> list[str]:
    """``table.index`` names of plain indexes on a unique key's leading columns."""
    redundant = []
    for table in metadata.tables.values():
        uniques = [
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ] + [
            cols
            for index in table.indexes
            if index.unique and (cols := _plain_columns(index))
        ]
        for index in table.indexes:
            cols = _plain_columns(index)
            if index.unique or not cols:
                continue
            if any(u[: len(cols)] == cols for u in uniques):
                redundant.append(f"{table.name}.{index.name}")
    return sorted(redundant)


#: ``info`` key of a unique constraint / index (or a primary-key column) that
#: is deliberately unique across tenants; its value is the reason.
GLOBAL_UNIQUE_INFO = "sandloom_global_unique"


def _surrogate(columns: list[Any]) -> bool:
    """One generated column (a client or server default, or autoincrement)."""
    if len(columns) != 1:
        return False
    column = columns[0]
    return (
        column.default is not None
        or column.server_default is not None
        or (column.autoincrement is True)
    )


def tenant_keys_without_tenant(
    metadata: MetaData, tenant_tables: set[str]
) -> list[str]:
    """``table: key`` of every natural key of a tenant table that does not
    contain ``tenant_id``: a primary key that is not one generated column, a
    unique constraint or a unique index, unless marked with
    ``info={GLOBAL_UNIQUE_INFO: "<reason>"}``."""
    found: list[str] = []
    for name in sorted(tenant_tables):
        table = metadata.tables.get(name)
        if table is None:
            continue
        pk = list(table.primary_key.columns)
        names = [c.name for c in pk]
        waived = any(GLOBAL_UNIQUE_INFO in c.info for c in pk)
        if pk and "tenant_id" not in names and not _surrogate(pk) and not waived:
            found.append(f"{name}: primary key {tuple(names)}")
        keys = [
            (c.name or "unique", list(c.columns), c.info)
            for c in table.constraints
            if isinstance(c, UniqueConstraint)
        ] + [
            (i.name or "unique index", list(i.columns), i.info)
            for i in table.indexes
            if i.unique
        ]
        for key_name, columns, info in keys:
            names = [c.name for c in columns]
            waived = GLOBAL_UNIQUE_INFO in info or any(
                GLOBAL_UNIQUE_INFO in c.info for c in columns
            )
            if "tenant_id" not in names and not waived:
                found.append(f"{name}: unique key {key_name} {tuple(names)}")
    return found


__all__ = [
    "assert_disposable_test_database",
    "open_test_engine",
    "open_test_session",
    "GLOBAL_UNIQUE_INFO",
    "redundant_indexes",
    "tenant_keys_without_tenant",
    "reset_schema",
]
