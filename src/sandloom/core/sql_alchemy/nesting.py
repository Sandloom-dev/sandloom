"""How a unit opened inside another unit of the same task gets its transaction.

Two policies, chosen once per engine from the renderer's ``serial_writers``
fact (:func:`nesting_policy`, cached on the sync engine) and asked by
:class:`sandloom.core.sql_alchemy.uow.SQLAlchemyUnitOfWork` at its entry:

* :data:`INDEPENDENT_UNITS` (concurrent writers: PostgreSQL; and every
  read-only unit): every unit opens its own transaction and commits on its
  own. No lookup.
* :class:`JoinCallerAsSavepoint` (serial writers: SQLite). A unit opened
  while a unit of the SAME task holds the write lock of the SAME database (a
  local contract call, a saga step, a child ``open_uow()``) would wait for its
  own caller on a second connection (SANDLOOM-R016). It runs on the caller's
  connection inside a SAVEPOINT instead: its own session (its own outbox,
  hooks and admission), whose commit releases the savepoint and whose
  rollback rolls back to it. It is not durable on its own: the caller's
  COMMIT commits it and the caller's rollback undoes it, so its post-commit
  stages wait for the caller's
  (:func:`sandloom.core.unit_of_work.defer_to_host`). A unit on another
  database instance, a read-only unit and a unit whose caller holds no lock
  yet open their own transaction.

Spec ``design/infra-agnostic/spec.md``: "Slice 8c, as built" and "Risk fix 6,
as built" (guarantee G-UOW-09).
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine.base import OptionEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from sandloom.core.sql_alchemy.portable import (
    WRITE_LOCK_HELD,
    WRITE_LOCK_TASK,
    dialect_name,
    serial_writers,
)
from sandloom.core.sql_alchemy.write_admission import UNIT_ATTR
from sandloom.core.unit_of_work.request import CLOSED_KEY, OWNER_KEY, _task
from sandloom.core.unit_of_work.scope import (
    _NESTED_HOST_KEY,
    JOINED_COUNT_KEY,
    ROLLBACK_EXPECTED_KEY,
    JoinedUnitRolledBack,
    _active_uow_sessions,
    _current_uow_session,
)

#: Sync-engine attribute caching the engine's nesting policy.
_POLICY_ATTR = "_sandloom_nesting_policy"
#: A factory without ``kw`` (a plain function): no bind.
NO_FACTORY_KW: dict[str, Any] = {}
#: Session-factory attribute caching ``(its bind, the policy)``: a unit reads
#: it with one attribute access (:func:`factory_policy`).
FACTORY_POLICY_ATTR = "_sandloom_unit_nesting"


class IndependentUnits:
    """Every unit opens a transaction of its own (concurrent writers)."""

    __slots__ = ()

    #: ``False``: the unit of work skips :meth:`enter` (no lookup at all).
    nests = False

    def host_for(self, session_factory: Any) -> AsyncSession | None:
        return None

    async def enter(self, uow: Any) -> bool:
        return False


#: The policy of a database whose writers run concurrently, and of every
#: engine with options of its own (a read-only unit's).
INDEPENDENT_UNITS = IndependentUnits()


def nesting_policy(session_factory: Any) -> IndependentUnits | JoinCallerAsSavepoint:
    """The nesting policy of ``session_factory``'s engine, computed at the
    engine's first unit and cached on it."""
    bind = getattr(session_factory, "kw", {}).get("bind")
    engine = getattr(bind, "sync_engine", None)
    if engine is None:
        return INDEPENDENT_UNITS
    policy = getattr(engine, _POLICY_ATTR, None)
    if policy is None:
        if isinstance(engine, OptionEngine) or not serial_writers(dialect_name(engine)):
            # an engine with options of its own (a read-only unit's) begins
            # its own kind of transaction: never nested
            policy = INDEPENDENT_UNITS
        else:
            policy = JoinCallerAsSavepoint(engine)
        try:
            setattr(engine, _POLICY_ATTR, policy)
        except AttributeError:  # pragma: no cover - an engine without attributes
            pass
    return policy


def factory_policy(session_factory: Any) -> IndependentUnits | JoinCallerAsSavepoint:
    """:func:`nesting_policy`, cached on the session factory as well (keyed
    by its bind, which a factory keeps): what a unit asks at its entry."""
    bind = getattr(session_factory, "kw", NO_FACTORY_KW).get("bind")
    policy = nesting_policy(session_factory)
    try:
        setattr(session_factory, FACTORY_POLICY_ATTR, (bind, policy))
    except AttributeError:  # pragma: no cover - a factory without attributes
        pass
    return policy


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


#: Sessions the test harness pins as hosts in this context (:func:`pin_test_host`).
_PINNED: ContextVar[tuple[Any, ...]] = ContextVar(
    "sandloom_pinned_test_hosts", default=()
)


@contextmanager
def pin_test_host(session: Any) -> Iterator[None]:
    """Test seam (the harness's ``db`` fixture): while ``session``'s
    transaction holds the write lock of a database whose writers are serial,
    a unit of the task that took it nests in it, even where the session is
    not one of the context's open units (a request's isolated scope, an
    operation-intent route's own units). The ``client`` fixture shares the
    session with requests already; this extends it to the units they open,
    instead of refusing them (SANDLOOM-R016).

    The pin holds in the current context and the tasks it starts, which copy
    it (a ``ContextVar``): a pinned host of one test is invisible to another.
    A task started in a fresh ``contextvars.Context`` (the in-memory bus's
    consumer deliveries, the post-commit dispatcher's marks) does not see
    the pin either: its units do not nest in the session, they wait for its
    write lock like any other writer (before risk fix 6 the pin was
    module-global and they nested)."""
    token = _PINNED.set((*_PINNED.get(), session))
    try:
        yield
    finally:
        try:
            _PINNED.reset(token)
        except ValueError:
            # left in another context than the one it was entered in (a
            # fixture's teardown): drop this session only
            _PINNED.set(tuple(s for s in _PINNED.get() if s is not session))


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
    """The unit a new unit of ``session_factory`` nests in under its engine's
    policy, or ``None`` (a transaction of its own; PostgreSQL: always)."""
    cached = getattr(session_factory, FACTORY_POLICY_ATTR, None)
    policy: IndependentUnits | JoinCallerAsSavepoint
    bind = getattr(session_factory, "kw", NO_FACTORY_KW).get("bind")
    if cached is not None and cached[0] is bind:
        policy = cached[1]
    else:
        policy = factory_policy(session_factory)
    if not policy.nests:
        return None
    return policy.host_for(session_factory)


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


class _NestedUnit:
    """A unit's place in its caller's transaction: the caller's connection,
    the unit the connection was attributed to before (the caller's), and the
    host's gate this unit holds. Closing it hands all three back."""

    __slots__ = ("connection", "gate", "previous")

    def __init__(self, connection: Any, previous: Any, gate: _NestingGate) -> None:
        self.connection = connection
        self.previous = previous
        self.gate = gate

    async def close(self, session: AsyncSession) -> None:
        try:
            await session.close()
        finally:
            # the caller's statements are the caller's unit's again
            if self.previous is None:
                vars(self.connection).pop(UNIT_ATTR, None)
            else:
                setattr(self.connection, UNIT_ATTR, self.previous)
            self.gate.release()


class JoinCallerAsSavepoint:
    """A unit of a task whose open unit holds the database's write lock runs
    as a SAVEPOINT on that unit's connection (serial writers; module doc)."""

    __slots__ = ("engine",)

    nests = True

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def host_for(self, session_factory: Any) -> AsyncSession | None:
        """The innermost open unit of the current context, owned by this
        task, whose transaction holds the write lock of this database (else
        a session the test harness pinned, whose lock this task or a
        finished one took), or ``None``."""
        current = _current_uow_session.get()
        active = _active_uow_sessions.get()
        pinned = _PINNED.get()
        if current is None and not active and not pinned:
            return None
        engine = self.engine
        task = _task()
        for host in (current, *reversed(active)):
            sync = getattr(host, "sync_session", None)
            if sync is None:
                continue
            info = sync.info
            if info.get(CLOSED_KEY) or info.get(OWNER_KEY) not in (None, task):
                continue
            if _write_connection(sync, engine) is not None:
                return host
        for host in reversed(pinned):
            sync = getattr(host, "sync_session", None)
            if sync is not None and _holds_for_task(sync, engine, task):
                return host  # type: ignore[no-any-return]
        return None

    async def enter(self, uow: Any) -> bool:
        """Open ``uow`` as a SAVEPOINT on its host's connection: its commit
        releases the savepoint, its rollback rolls back to it, and closing it
        leaves the caller's transaction open. ``False`` when there is no host,
        or the host released its lock while this task waited at its gate (the
        unit then opens a transaction of its own).

        The host's transaction must be usable: a host whose flush failed
        (pending rollback) raises ``PendingRollbackError`` here, as its own
        next statement would."""
        factory = uow._session_factory
        host = self.host_for(factory)
        if host is None:
            return False
        gate = _gate(host)
        await gate.acquire(_task())
        if _write_connection(host.sync_session, self.engine) is None:
            gate.release()
            return False
        try:
            connection = await host.connection()
            session = factory(bind=connection, join_transaction_mode="create_savepoint")
            session.info[_NESTED_HOST_KEY] = host
            _watch_host_rollback(host)
            sync_connection = connection.sync_connection
            nested = _NestedUnit(
                sync_connection, getattr(sync_connection, UNIT_ATTR, None), gate
            )
        except BaseException:
            gate.release()
            raise
        try:
            await session.begin()
            await session.connection()  # the SAVEPOINT, now
        except BaseException:
            await nested.close(session)
            raise
        uow._nested = nested
        uow.session = session
        return True


__all__ = [
    "INDEPENDENT_UNITS",
    "IndependentUnits",
    "JoinCallerAsSavepoint",
    "nesting_policy",
    "factory_policy",
    "FACTORY_POLICY_ATTR",
    "NO_FACTORY_KW",
    "pin_test_host",
    "savepoint_host",
]


#: ``host.info`` key: the host's COMMIT and rollback are watched (once).
_JOINED_WATCHED_KEY = "sandloom_joined_watched"


def _watch_host_rollback(host: AsyncSession) -> None:
    """Warn when ``host`` rolls back after units joined its transaction
    (:class:`~sandloom.core.unit_of_work.scope.JoinedUnitRolledBack`)."""
    sync = host.sync_session
    if sync.info.get(_JOINED_WATCHED_KEY):
        return
    event.listen(sync, "after_commit", _forget_joined)
    event.listen(sync, "after_rollback", _joined_rolled_back)
    sync.info[_JOINED_WATCHED_KEY] = True


def _forget_joined(sync_session: Session) -> None:
    sync_session.info.pop(JOINED_COUNT_KEY, None)


def _joined_rolled_back(sync_session: Session) -> None:
    count = sync_session.info.pop(JOINED_COUNT_KEY, 0)
    if not count or sync_session.info.get(ROLLBACK_EXPECTED_KEY):
        return
    warnings.warn(
        JoinedUnitRolledBack(
            f"{count} unit(s) that joined this transaction (a contract command, "
            "a saga step or a child open_uow() on a serial-writer database) "
            "rolled back with it. On PostgreSQL they committed on their own and "
            "stay committed: do not rely on this rollback; make the call a saga "
            "step or undo it explicitly (G-UOW-09)"
        ),
        stacklevel=2,
    )
