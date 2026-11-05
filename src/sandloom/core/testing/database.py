"""Fast per-test database reset for module test suites.

Recreating the schema (``drop_all`` + ``create_all`` over every table in
``Base.metadata``) before each test costs ~0.8s. :func:`reset_schema` creates
the schema once per process and database; later calls empty it again.

How a database is emptied is its own (:mod:`sandloom.core.drivers.schema_reset`,
chosen by the engine's database). On PostgreSQL ``TRUNCATE`` gives every table
a new file (plus its indexes and TOAST), so truncating ALL tables costs ~0.1 s
on an idle server and several times that under load — paid by every test. A
test touches a few tables, so the reset first asks which tables hold a row
(one capped ``SELECT`` per table in a single statement, with ``row_security =
off`` so a row-level-security policy can never hide a row — the statement
errors instead and the reset falls back to truncating everything),
``DELETE``s those children-first (a table holding many rows is truncated
instead) and rewinds every used sequence (a rolled-back insert still consumes
a value). The result is the same as truncating every table: all of them
empty, every owned sequence at its start. On SQLite (a file per run or xdist
worker, ``SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///…``) every table is
``DELETE``d children-first and the ``AUTOINCREMENT`` counters start again.

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
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, UniqueConstraint
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import sandloom.core.sql_alchemy.models
import sandloom.core.sql_alchemy.models.audit  # noqa: F401
from sandloom.core._internal.db_provider import (
    build_disposable_engine,
    schema_reset_for,
)
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.uow import pin_test_host
from sandloom.core.testing.connections import (
    TEST_ENGINE_MAX_OVERFLOW,
    TEST_ENGINE_POOL_SIZE,
)
from sandloom.core.testing.ownership import claim_database

logger = logging.getLogger(__name__)

# (database url, metadata identity, table set) the schema was last built for
# in this process. A new table registered since then forces a rebuild.
_built: set[tuple[str, int, frozenset[str]]] = set()
# The sequences owned by the tables of a built schema (rewound on reset).
_sequences: dict[tuple[str, int, frozenset[str]], list[str]] = {}


def _key(engine: AsyncEngine, metadata: MetaData) -> tuple[str, int, frozenset[str]]:
    url = engine.url.render_as_string(hide_password=False)
    return (url, id(metadata), frozenset(metadata.tables))


async def _rebuild(engine: AsyncEngine, metadata: MetaData) -> None:
    await schema_reset_for(engine).rebuild(engine, metadata)


#: A dirty table with more rows than this is TRUNCATEd instead of DELETEd.
DELETE_ROW_LIMIT = 1000


async def _truncate_dirty(
    engine: AsyncEngine, metadata: MetaData, sequences: list[str]
) -> None:
    """Empty the tables that hold a row and rewind every used sequence, the
    database's own way (:mod:`sandloom.core.drivers.schema_reset`). Any error
    propagates and the caller empties everything."""
    await schema_reset_for(engine).empty_dirty(
        engine, metadata, sequences, row_limit=DELETE_ROW_LIMIT
    )


async def _truncate_all(engine: AsyncEngine, metadata: MetaData) -> None:
    await schema_reset_for(engine).empty_all(engine, metadata)


async def reset_schema(engine: AsyncEngine, metadata: MetaData) -> None:
    """Leave every table of ``metadata`` present and empty, sequences rewound."""
    key = _key(engine, metadata)
    if key in _built and metadata.sorted_tables:
        try:
            await _truncate_dirty(engine, metadata, _sequences.get(key, []))
            return
        except DBAPIError:
            logger.debug("dirty-table reset failed; truncating all", exc_info=True)
        try:
            await _truncate_all(engine, metadata)
            return
        except DBAPIError:
            logger.debug("truncate failed; rebuilding test schema", exc_info=True)
            _built.discard(key)
    await _rebuild(engine, metadata)
    _built.add(key)
    _sequences[key] = await schema_reset_for(engine).owned_sequences(engine, metadata)


def assert_disposable_test_database(database_url: str) -> None:
    """Refuse to reset anything but an obviously disposable TEST database.

    The test harness empties every table on ``database_url``; a shell that still
    exports a dev/prod ``DATABASE_URL`` must never get that far. A database
    name containing ``test`` is required even when ``ENVIRONMENT=test`` (for a
    SQLite file: its file name, never a directory above it).
    """
    db_name = _database_name(database_url)
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
    if intended and not is_run_database(database_url, intended):
        raise RuntimeError(
            f"Refusing to reset tables on database {db_name!r}: this run "
            f"set SANDLOOM_TEST_DATABASE_URL to database "
            f"{_database_name(intended)!r}."
        )


def _database_name(url: str) -> str:
    parsed = make_url(url)
    name = parsed.database or ""
    if parsed.get_backend_name() == "sqlite":
        name = Path(name).name
    return name.lower()


def is_run_database(url: str, intended: str) -> bool:
    """``url`` is the database ``intended`` names, or an xdist worker's copy
    of it (``<name>_gw<N>``; a SQLite file ``<stem>_gw<N><suffix>`` in the
    same directory)."""
    got, want = make_url(url), make_url(intended)
    if got.get_backend_name() == "sqlite" or want.get_backend_name() == "sqlite":
        if got.get_backend_name() != want.get_backend_name():
            return False
        have = Path(got.database or "").resolve()
        named = Path(want.database or "").resolve()
        if have == named:
            return True
        return (
            have.parent == named.parent
            and _is_worker_copy(have.stem.lower(), named.stem.lower())
            and have.suffix == named.suffix
        )
    return (
        _is_worker_copy((got.database or "").lower(), (want.database or "").lower())
        or (got.database or "").lower() == (want.database or "").lower()
    )


def _is_worker_copy(name: str, base: str) -> bool:
    return name == base or name.startswith(f"{base}_gw")


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
        # a test's own sessions (SQLite: begun at their first write, so a
        # read never waits for the ``db`` fixture's write lock)
        yield schema_reset_for(engine).caller_engine(engine)
    finally:
        await engine.dispose()


@asynccontextmanager
async def open_test_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on ``engine`` that is rolled back when the test ends. It
    stands in for the request's unit (SQLite: its transaction begins at its
    first write, which takes the write lock nested units then join)."""
    factory = async_sessionmaker(
        schema_reset_for(engine).unit_engine(engine), expire_on_commit=False
    )
    async with factory() as session:
        try:
            # a unit the test's task opens while this session holds the
            # write lock of a database whose writers are serial nests in it
            with pin_test_host(session):
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
    "is_run_database",
    "open_test_engine",
    "open_test_session",
    "GLOBAL_UNIQUE_INFO",
    "redundant_indexes",
    "tenant_keys_without_tenant",
    "reset_schema",
]
