"""aiosqlite's DBAPI adapter: how a SQLite connection opens and begins.

SQLite is the second database, for development and tests
(``design/infra-agnostic/spec.md``, section 6). Its SQL is the renderer's
(:class:`~sandloom.core.drivers.sql_dialects.SqliteSql`); this adapter
installs what makes that renderer's features true on an engine:

* **Implicit transactions off.** pysqlite (which aiosqlite wraps) begins a
  transaction on its own before a write and never before a read, and emits no
  ``SAVEPOINT``-safe ``BEGIN``. The connection's ``isolation_level`` is set to
  ``None`` and the engine's ``begin`` event emits the unit's ``BEGIN`` itself:
  SQLAlchemy's documented recipe, so ``begin_nested()`` and the rolled-back
  test fixture work.
* **Serial writers.** A unit begins with its ``transaction_options``'
  ``BEGIN`` text, else the renderer's write ``BEGIN IMMEDIATE``: every unit
  not declared read-only takes the write lock at its first statement, reads
  before its first write included. The transaction is marked
  (:data:`~sandloom.core.sql_alchemy.portable.WRITE_LOCK_HELD`) so the
  kernel's transaction locks succeed inside it.
* **Read-only units** begin deferred (one WAL snapshot) and turn
  ``PRAGMA query_only`` on until they end.
* **Each connection opens with** ``busy_timeout`` (a writer waits for the
  lock that long; the runtime statement timeout, else 5 s), ``foreign_keys``
  and, for a file database, WAL.
* An ``AUTOCOMMIT`` connection (one statement, its own transaction) begins
  nothing.

In-memory databases (``sqlite://``, ``:memory:``, ``mode=memory``) have no
WAL and no second connection: they are refused (SANDLOOM-B032), except a test
engine on one shared connection (``StaticPool``), and never advertise
``snapshot_reads`` or ``transaction_locks``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import weakref
from collections.abc import MutableMapping
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url

from sandloom.core.drivers.sql_dialects import (
    SQLITE_BEGIN_OPTION,
    SQLITE_QUERY_ONLY_OPTION,
    SQLITE_WRITE_BEGIN,
)
from sandloom.core.sql_alchemy.dbapi import DbapiAdapter
from sandloom.core.sql_alchemy.portable import WRITE_LOCK_HELD

#: The busy timeout when the runtime statement timeout is off (0).
DEFAULT_BUSY_TIMEOUT_MS = 5_000
#: The features an in-memory database never advertises: one connection, so
#: no snapshot a writer could change and no second writer to exclude.
MEMORY_WITHHELD_FEATURES = frozenset({"snapshot_reads", "transaction_locks"})

#: Connection ``info`` key: ``PRAGMA query_only`` is on (a read-only unit).
_QUERY_ONLY = "sandloom.sqlite_query_only"


#: Per database file: the task whose unit holds its write lock, and that
#: connection's ``info`` (the mark is checked there, so a connection that
#: died without its COMMIT / ROLLBACK event leaves no stale holder).
_WRITERS: dict[
    str, tuple[weakref.ref[asyncio.Task[Any]], MutableMapping[Any, Any]]
] = {}


class NestedWriterRefused(RuntimeError):
    """A second unit of the same task began a write on a database whose
    writers are serial while the task's first unit holds the write lock on
    another connection: it would wait for itself until ``busy_timeout``
    (SANDLOOM-R016)."""

    code = "SANDLOOM-R016"

    def __init__(self, database: str) -> None:
        super().__init__(
            f"a second unit of this task began a write on the SQLite database "
            f"{database!r} while the task's own unit holds its write lock on "
            "another connection (a local contract call, a saga step or a "
            "child open_uow() inside a unit): SQLite has one writer, so it "
            "would wait for itself until busy_timeout [SANDLOOM-R016]. Do the "
            "nested write in the caller's unit, run it after the caller's "
            "unit commits, or run the database on PostgreSQL"
        )


class SqliteUnavailable(RuntimeError):
    """The driver cannot open a configured SQLite database: in memory, no
    async DBAPI named, or aiosqlite not installed (SANDLOOM-B032)."""

    fatal_at_startup = True


def is_sqlite_url(url: Any) -> bool:
    """True when ``url`` names a SQLite database."""
    return make_url(str(url)).get_backend_name() == "sqlite"


def in_memory(url: Any) -> bool:
    """True when the SQLite ``url`` is an in-memory database: no file
    (``sqlite://``), ``:memory:``, or a ``mode=memory`` URI."""
    parsed = make_url(str(url))
    database = parsed.database or ""
    return (
        database in ("", ":memory:")
        or database.startswith("file::memory:")
        or parsed.query.get("mode") == "memory"
        or "mode=memory" in database
    )


def aiosqlite_installed() -> bool:
    """True when the optional ``aiosqlite`` DBAPI is importable."""
    return importlib.util.find_spec("aiosqlite") is not None


def sqlite_url_problem(url: Any, *, allow_memory: bool = False) -> str | None:
    """Why the driver cannot open the SQLite database ``url``
    (SANDLOOM-B032), or ``None`` (also for any other database). Build-free:
    the URL and the installed packages only."""
    if not is_sqlite_url(url):
        return None
    parsed = make_url(str(url))
    if parsed.get_driver_name() != "aiosqlite":
        return (
            f"the SQLite URL {parsed.render_as_string(hide_password=True)!r} "
            "names no async DBAPI: use sqlite+aiosqlite:///<file> "
            "[SANDLOOM-B032]"
        )
    if not aiosqlite_installed():
        return (
            "a SQLite database needs the aiosqlite DBAPI, which is not "
            "installed: pip install 'sandloom[sqlite]' [SANDLOOM-B032]"
        )
    if in_memory(url) and not allow_memory:
        return (
            "an in-memory SQLite database has no WAL and no second "
            "connection, so a second unit, the relay or a worker would see "
            "another, empty database: use a file, "
            "sqlite+aiosqlite:///<path>.db [SANDLOOM-B032]"
        )
    return None


def _connect(pragmas: tuple[str, ...], dbapi_connection: Any, _record: Any) -> None:
    # pysqlite begins no transaction of its own from here on.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        for pragma in pragmas:
            cursor.execute(pragma)
    finally:
        cursor.close()


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running loop (a sync caller)
        return None


def _refuse_a_nested_writer(database: str, info: MutableMapping[Any, Any]) -> None:
    """Raise :class:`NestedWriterRefused` when this task's unit already holds
    ``database``'s write lock on another connection."""
    holder = _WRITERS.get(database)
    if holder is None:
        return
    task_ref, held = holder
    task = _current_task()
    if task is None or held is info or not held.get(WRITE_LOCK_HELD):
        return
    if task_ref() is task:
        raise NestedWriterRefused(database)


def _release(database: str | None, info: MutableMapping[Any, Any]) -> None:
    info.pop(WRITE_LOCK_HELD, None)
    if database is not None:
        holder = _WRITERS.get(database)
        if holder is not None and holder[1] is info:
            del _WRITERS[database]


def _begin(database: str | None, conn: Any) -> None:
    options = conn.get_execution_options()
    info = conn.info
    # A mark left by a transaction whose end event never ran is never kept.
    _release(database, info)
    query_only = bool(options.get(SQLITE_QUERY_ONLY_OPTION))
    if info.get(_QUERY_ONLY) and not query_only:
        # A read-only unit's pragma, not reset at its end (it ended with an
        # error): this connection writes again.
        conn.exec_driver_sql("PRAGMA query_only = OFF")
        info.pop(_QUERY_ONLY, None)
    if options.get("isolation_level") == "AUTOCOMMIT":
        return  # each statement is its own transaction
    dbapi_connection = conn.connection.dbapi_connection
    if dbapi_connection.isolation_level is not None:
        # SQLAlchemy's reset of a connection that ran under another isolation
        # level turns pysqlite's implicit BEGIN back on.
        dbapi_connection.isolation_level = None
    begin = options.get(SQLITE_BEGIN_OPTION) or SQLITE_WRITE_BEGIN
    writer = database if begin == SQLITE_WRITE_BEGIN else None
    if writer is not None:
        _refuse_a_nested_writer(writer, info)
    conn.exec_driver_sql(begin)
    if writer is not None:
        info[WRITE_LOCK_HELD] = True
        task = _current_task()
        if task is not None:
            _WRITERS[writer] = (weakref.ref(task), info)
    if query_only and not info.get(_QUERY_ONLY):
        conn.exec_driver_sql("PRAGMA query_only = ON")
        info[_QUERY_ONLY] = True


def _commit(database: str | None, conn: Any) -> None:
    info = conn.info
    _release(database, info)
    if info.pop(_QUERY_ONLY, False):
        # Before COMMIT, still inside the read-only transaction.
        conn.exec_driver_sql("PRAGMA query_only = OFF")


def _rollback(database: str | None, conn: Any) -> None:
    # No statement here (the transaction may be failing): a pragma left on
    # is turned off by this connection's next BEGIN.
    _release(database, conn.info)


class AiosqliteAdapter(DbapiAdapter):
    """aiosqlite: the plain path, plus the engine setup of the module doc."""

    name = "aiosqlite"

    def config_problem(self, url: str) -> str | None:
        """An in-memory database, or aiosqlite not installed (SANDLOOM-B032)."""
        return sqlite_url_problem(url)

    def prepare_engine(self, engine: Any, *, statement_timeout_ms: int) -> None:
        """Implicit transactions off, the unit's ``BEGIN``, the connection
        pragmas (see the module doc). An in-memory test engine (one shared
        connection) marks no write lock: it has no transaction locks."""
        sync_engine = getattr(engine, "sync_engine", engine)
        busy = int(statement_timeout_ms or DEFAULT_BUSY_TIMEOUT_MS)
        pragmas = [f"PRAGMA busy_timeout = {busy}", "PRAGMA foreign_keys = ON"]
        memory = in_memory(sync_engine.url)
        if not memory:
            pragmas.append("PRAGMA journal_mode = WAL")
        database = None if memory else _database_key(sync_engine.url)
        event.listen(sync_engine, "connect", partial(_connect, tuple(pragmas)))
        event.listen(sync_engine, "begin", partial(_begin, database))
        event.listen(sync_engine, "commit", partial(_commit, database))
        event.listen(sync_engine, "rollback", partial(_rollback, database))


def _database_key(url: Any) -> str:
    """The database file a URL names, resolved (two engines on one file
    share its write lock)."""
    return str(Path(make_url(str(url)).database or "").resolve())


#: The adapter the SQLAlchemy driver registers for ``sqlite+aiosqlite``.
AIOSQLITE = AiosqliteAdapter()


__all__ = [
    "AIOSQLITE",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MEMORY_WITHHELD_FEATURES",
    "AiosqliteAdapter",
    "NestedWriterRefused",
    "SqliteUnavailable",
    "aiosqlite_installed",
    "in_memory",
    "is_sqlite_url",
    "sqlite_url_problem",
]
