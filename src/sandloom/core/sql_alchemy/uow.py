"""SQLAlchemy implementation of UnitOfWorkContract.

Note: This module is intentionally placed under core/sql_alchemy as a
transition step. Eventually, all SQL-specific pieces should be moved into a
plugin implementing the same abstract contracts.

**Nested units on a database whose writers are serial** (the renderer's
``serial_writers`` fact: SQLite). A unit opened while a unit of the SAME task
holds the write lock of the SAME database (a local contract call, a saga step,
a child ``open_uow()``) would wait for its own caller on a second connection
(SANDLOOM-R016). It runs on the caller's connection inside a SAVEPOINT
instead: its own session (its own outbox, hooks and admission), whose commit
releases the savepoint and whose rollback rolls back to it. It is not durable
on its own: the caller's COMMIT commits it and the caller's rollback undoes
it, so its post-commit stages wait for the caller's
(:func:`sandloom.core.unit_of_work.defer_to_host`). Every other database, a
unit on another database instance and a read-only unit open their own
transaction as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any

from sqlalchemy.engine.base import OptionEngine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.sql_alchemy.portable import (
    WRITE_LOCK_HELD,
    WRITE_LOCK_TASK,
    dialect_name,
    serial_writers,
)
from sandloom.core.sql_alchemy.write_admission import UNIT_ATTR, mark_unit_closed
from sandloom.core.unit_of_work.hooks import _run_before_commit_callbacks
from sandloom.core.unit_of_work.request import (
    CLOSED_KEY,
    OWNER_KEY,
    _task,
    release_unit,
)
from sandloom.core.unit_of_work.scope import (
    _NESTED_HOST_KEY,
    _active_uow_sessions,
    _current_uow_session,
)

#: Sync-engine attribute caching the renderer's ``serial_writers`` fact.
_SERIAL_ATTR = "_sandloom_serial_writers"


def _serial_engine(session_factory: Any) -> Any:
    """The sync engine of ``session_factory``'s bind when its database's
    writers are serial, else ``None`` (cached on the engine)."""
    bind = getattr(session_factory, "kw", {}).get("bind")
    engine = getattr(bind, "sync_engine", None)
    if engine is None or isinstance(engine, OptionEngine):
        # an engine with options of its own (a read-only unit's) begins
        # its own kind of transaction: never nested
        return None
    serial = getattr(engine, _SERIAL_ATTR, None)
    if serial is None:
        serial = serial_writers(dialect_name(engine))
        try:
            setattr(engine, _SERIAL_ATTR, serial)
        except AttributeError:  # pragma: no cover - an engine without attributes
            pass
    return engine if serial else None


def _in_memory(engine: Any) -> bool:
    """An engine on an in-memory database: two such engines with one URL are
    two databases, so only engine identity says "the same database"."""
    database = engine.url.database
    return not database or database == ":memory:" or "mode=memory" in str(engine.url)


def _write_connection(sync_session: Any, engine: Any) -> Any:
    """The connection of ``sync_session``'s open transaction on the database
    of ``engine`` (the same engine, or one on the same URL: the same database
    instance) that holds its write lock, or ``None``. No I/O: a session whose
    transaction has not reached the database yet holds no lock."""
    transaction = sync_session.get_transaction()
    if transaction is None:
        return None
    for entry in getattr(transaction, "_connections", {}).values():
        conn = entry[0]
        same = conn.engine is engine or (
            conn.engine.url == engine.url and not _in_memory(engine)
        )
        if same and not conn.closed and conn.info.get(WRITE_LOCK_HELD):
            return conn
    return None


#: Sessions the test harness pins as hosts (:func:`pin_test_host`).
_PINNED: list[Any] = []


@contextmanager
def pin_test_host(session: Any) -> Iterator[None]:
    """Test seam (the harness's ``db`` fixture): while ``session``'s
    transaction holds the write lock of a database whose writers are serial,
    a unit of the task that took it nests in it, even where the session is
    not one of the context's open units (a request's isolated scope, an
    operation-intent route's own units). The ``client`` fixture shares the
    session with requests already; this extends it to the units they open,
    instead of refusing them (SANDLOOM-R016)."""
    _PINNED.append(session)
    try:
        yield
    finally:
        _PINNED.remove(session)


def _holds_for_task(sync: Any, engine: Any, task: Any) -> bool:
    conn = _write_connection(sync, engine)
    if conn is None:
        return False
    ref = conn.info.get(WRITE_LOCK_TASK)
    if ref is None:
        return False
    holder = ref()
    # this task's lock, or one a finished task took (an async fixture's
    # setup): then any task of the test may nest, one at a time (the host's
    # nesting gate), never a task beside the live one that holds it
    return holder is task or holder is None or holder.done()


def savepoint_host(session_factory: Any) -> AsyncSession | None:
    """The unit of this task a new unit of ``session_factory`` nests in: on
    a database whose writers are serial, the innermost open unit of the
    current context, owned by this task, whose transaction holds the write
    lock of the same database (else a session the test harness pinned,
    whose lock this task or a finished one took). ``None`` everywhere else (PostgreSQL:
    always)."""
    if (
        _current_uow_session.get() is None
        and not _active_uow_sessions.get()
        and not _PINNED
    ):
        return None
    engine = _serial_engine(session_factory)
    if engine is None:
        return None
    task = _task()
    for host in (_current_uow_session.get(), *reversed(_active_uow_sessions.get())):
        sync = getattr(host, "sync_session", None)
        if sync is None:
            continue
        info = sync.info
        if info.get(CLOSED_KEY) or info.get(OWNER_KEY) not in (None, task):
            continue
        if _write_connection(sync, engine) is not None:
            return host
    for host in reversed(_PINNED):
        sync = getattr(host, "sync_session", None)
        if sync is not None and _holds_for_task(sync, engine, task):
            return host  # type: ignore[no-any-return]
    return None


#: ``session.info`` key of a host: the gate of its nested units.
_GATE_KEY = "sandloom_nesting_gate"


class _NestingGate:
    """One task at a time nests in a host: the units of one task nest in
    each other (re-entrant), another task waits until they ended. A savepoint
    of a second task interleaved on the same connection would be released or
    rolled back by the first task's statements."""

    __slots__ = ("depth", "free", "owner")

    def __init__(self) -> None:
        self.owner: Any = None
        self.depth = 0
        self.free = asyncio.Event()
        self.free.set()

    async def acquire(self, task: Any) -> None:
        while self.depth and self.owner is not task:
            owner = self.owner
            if owner is None or owner.done():  # ended without its exit
                self.depth = 0
                break
            await self.free.wait()
        self.owner = task
        self.depth += 1
        self.free.clear()

    def release(self) -> None:
        self.depth -= 1
        if self.depth <= 0:
            self.depth = 0
            self.owner = None
            self.free.set()


def _gate(host: Any) -> _NestingGate:
    info = host.sync_session.info
    gate = info.get(_GATE_KEY)
    if gate is None:
        gate = info[_GATE_KEY] = _NestingGate()
    return gate  # type: ignore[no-any-return]


class SQLAlchemyUnitOfWork(UnitOfWorkContract):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory
        self.session: AsyncSession | None = None
        #: a nested unit's connection and the unit it was attributed to
        #: before (the caller's), handed back when this one ends
        self._host: tuple[Any, Any, _NestingGate] | None = None

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        host = savepoint_host(self._session_factory)
        if host is not None and await self._enter_nested(host):
            return self
        session = self._session_factory()
        try:
            await session.begin()
        except BaseException:
            # begin() can fail (pool exhausted, DB unreachable, pre-ping). If it
            # does, __aexit__ never runs, so close the session here to avoid
            # leaking it (and any checked-out connection) back into the pool.
            await session.close()
            raise
        self.session = session
        return self

    async def _enter_nested(self, host: AsyncSession) -> bool:
        """Open this unit as a SAVEPOINT on ``host``'s connection (module
        doc): its commit releases the savepoint, its rollback rolls back to
        it, and closing it leaves the caller's transaction open. ``False``
        when the host released its lock while this task waited at its gate
        (the unit then opens a transaction of its own).

        The host's transaction must be usable: a host whose flush failed
        (pending rollback) raises ``PendingRollbackError`` here, as its own
        next statement would."""
        gate = _gate(host)
        task = _task()
        await gate.acquire(task)
        engine = _serial_engine(self._session_factory)
        if engine is None or _write_connection(host.sync_session, engine) is None:
            gate.release()
            return False
        try:
            connection = await host.connection()
        except BaseException:
            gate.release()
            raise
        session = self._session_factory(
            bind=connection, join_transaction_mode="create_savepoint"
        )
        session.info[_NESTED_HOST_KEY] = host
        sync_connection = connection.sync_connection
        self._host = (sync_connection, getattr(sync_connection, UNIT_ATTR, None), gate)
        try:
            await session.begin()
            await session.connection()  # the SAVEPOINT, now
        except BaseException:
            await self._close_nested(session)
            raise
        self.session = session
        return True

    async def _close_nested(self, session: AsyncSession) -> None:
        try:
            await session.close()
        finally:
            host, self._host = self._host, None
            if host is not None:
                # the caller's statements are the caller's unit's again
                if host[1] is None:
                    vars(host[0]).pop(UNIT_ATTR, None)
                else:
                    setattr(host[0], UNIT_ATTR, host[1])
                host[2].release()

    def open_lazily(self) -> AsyncSession:
        """Construct the session WITHOUT beginning (no I/O): it autobegins at
        its first statement. The caller (the lazy request unit) commits or
        rolls back and then closes it (:meth:`close`)."""
        session = self._session_factory()
        self.session = session
        return session

    async def close(self) -> None:
        """Close the session and mark the unit exited (lazy request unit)."""
        session, self.session = self.session, None
        if session is not None:
            await self._close(session)
            release_unit(session)
            mark_unit_closed(session)

    async def _close(self, session: AsyncSession) -> None:
        if self._host is not None:
            await self._close_nested(session)
        else:
            await session.close()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.session:
            try:
                if exc:
                    await self.rollback()
                else:
                    await self._commit_after_checks()
            finally:
                # Always release the connection, even if commit/rollback raised.
                session, self.session = self.session, None
                await self._close(session)
                release_unit(session)
                mark_unit_closed(session)

    async def _commit_after_checks(self) -> None:
        """Run the session's ``before_commit`` checks, then commit.

        A failing check rolls the transaction back and propagates (the
        ``on_commit`` queue then never runs: nothing committed).
        """
        assert self.session is not None  # noqa: S101 - type narrowing of an invariant
        try:
            await _run_before_commit_callbacks(self.session)
        except BaseException:
            await self.rollback()
            raise
        await self.commit()

    async def commit(self) -> None:
        if self.session:
            await self.session.commit()

    async def rollback(self) -> None:
        if self.session:
            await self.session.rollback()
