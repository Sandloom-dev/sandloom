"""Units, ``db()`` and write admission of one request (spec §4.1-§4.4).

A **unit** is one Unit of Work: one transaction on one database. Every
UoW-opening path (the request unit of ``DbSession``, ``Database("x")``,
``db_session()`` blocks, contract units, consumer units) binds its session as
the innermost unit of the current context and records the task that OWNS it.

* :func:`db` returns the session of the innermost open unit of the current
  context. A task that does not own that unit is refused
  (:class:`ConcurrentSessionUseError`, SANDLOOM-R013) — child tasks inherit the
  context, so the common mistake is caught at its first ``db()``; a child that
  needs the database opens its own unit (``async with db_session() as s:``).
  In a request with no open unit, ``db()`` creates the LAZY request unit (no
  I/O: the connection is checked out at the first statement), owned by the
  request's handler task and committed / rolled back by the route runtime
  before the response is sent; from any other task it creates nothing.
* :class:`WriteAdmission` (one per request, shared with child tasks): the
  first unit that WRITES becomes the request's writer; a write by any other
  unit is refused before it reaches the cursor (:class:`MultiStoreWriteError`,
  SANDLOOM-R007). Contract, consumer, task, saga-step and kernel units run with a
  fresh admission scope (:func:`fresh_admission_scope`).
* :class:`RequestScope` carries the per-request state the route runtime
  (``sandloom.core.http.route_runtime``) installs: the handler task, the route
  policy, the admission, the lazy unit, the commit-time re-check arming and
  the operation-intent bookkeeping.

Passing a session OBJECT to another task is unsupported; the kernel session
class and the cursor hook detect it on a best-effort basis
(:mod:`sandloom.core.sql_alchemy.session_guard`).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError
from sandloom.core.unit_of_work.scope import (
    _DB_NAME_KEY,
    _active_uow_sessions,
    _current_uow_session,
)
from sandloom.core.unit_of_work.scope import _admission as _admission_var
from sandloom.core.unit_of_work.scope import _request_scope as _request_scope_var
from sandloom.core.unit_of_work.scope import stash_pending_check as _stash_pending_check

#: ``session.info`` key: the task that owns the unit (set when it opens).
OWNER_KEY = "sandloom_unit_owner"
#: ``session.info`` key: the unit has exited; any later use is refused.
CLOSED_KEY = "sandloom_unit_closed"
#: ``session.info`` key: write statements this unit executed (admission count).
WRITES_KEY = "sandloom_unit_writes"
#: ``session.info`` key: the request scope a unit was opened in (or ``None``).
SCOPE_KEY = "sandloom_request_scope"
#: ``session.info`` key: ``[(subject, savepoint token)]`` this unit's code
#: self-revoked after the live check of that subject passed (spec §4.5 item 1).
SELF_REVOKED_KEY = "sandloom_self_revoked"
#: ``session.info`` key: the scope whose re-check this transaction passed.
RECHECKED_KEY = "sandloom_rechecked"
#: ``session.info`` key: the unit is one of an operation-intent route's own
#: (sequential) units.
INTENT_UNIT_KEY = "sandloom_intent_unit"
#: ``session.info`` key: the surviving self-revoked subjects, fixed right
#: before COMMIT; moved to the request scope when the unit commits.
SELF_REVOKED_FINAL_KEY = "sandloom_self_revoked_final"


class ConcurrentSessionUseError(RuntimeError):
    """A task used a unit it does not own (SANDLOOM-R013)."""

    sandloom_code = "SANDLOOM-R013"


class MultiStoreWriteError(RuntimeError):
    """A second unit of the request tried to write (SANDLOOM-R007)."""

    sandloom_code = "SANDLOOM-R007"


class UnitRuleError(RuntimeError):
    """A unit, read scope or checkout started where the unit rules forbid it
    (SANDLOOM-R008)."""

    sandloom_code = "SANDLOOM-R008"


def _task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # not on an event loop thread
        return None


_current_task = _task


# --------------------------------------------------------------------------- #
# Write admission
# --------------------------------------------------------------------------- #
class WriteAdmission:
    """One writer per request (spec §4.4).

    ``write_store``: the route's write store when known (the first writer
    must be on it); ``secondaries``: databases ``allow_multi_db_write``
    admits as additional writers (best-effort 1PC). Units are identified by
    their kernel session (or, for a caller-owned session, its connection).
    """

    __slots__ = (
        "secondaries",
        "sequential",
        "write_store",
        "writer",
        "writer_database",
        "writers",
    )

    def __init__(
        self,
        write_store: str | None = None,
        secondaries: tuple[str, ...] = (),
        *,
        sequential: bool = False,
    ) -> None:
        self.write_store = write_store
        self.secondaries = frozenset(secondaries)
        #: operation-intent mode: sequential units (one open at a time,
        #: enforced when a unit opens) may each write (spec §4.4 rule 5)
        self.sequential = sequential
        #: the writing unit's serial (:func:`_unit_key`; never the unit
        #: itself: the unit's ``info`` references the request scope, which is
        #: this admission — holding the unit would make every writing request
        #: a GC cycle — and never its ``id()``, which a later unit may reuse)
        self.writer: int | None = None
        self.writer_database: str | None = None
        #: every admitted writing unit (serial) -> its database
        self.writers: dict[int, str | None] = {}

    def admit(self, unit: Any, database: str | None, what: str) -> None:
        """Admit a write by ``unit`` on ``database`` or raise SANDLOOM-R007."""
        key = _unit_key(unit)
        if key in self.writers or self.sequential:
            return
        store = self.write_store
        secondaries = self.secondaries
        if secondaries and database in secondaries and database != store:
            # an opted-out secondary is never the writer, whenever it writes:
            # writing it BEFORE the write store is the documented pattern
            # (secondaries commit first, the write store last)
            if any(db == database for db in self.writers.values()):
                raise MultiStoreWriteError(
                    f"{what}: a second unit writes the opted-out secondary "
                    f"{database!r} in this request; "
                    "one unit per database (SANDLOOM-R007)"
                )
            self.writers[key] = database
            return
        if self.writer is None:
            if store is not None and database is not None and database != store:
                raise MultiStoreWriteError(
                    f"{what} on database {database!r}, but this route's write store "
                    f"is {store!r}: do the request's writes in its write store, or "
                    f'declare writes("{database}") (SANDLOOM-R007)'
                )
            self.writer = key
            self.writer_database = database
            self.writers[key] = database
            return
        this = "" if database is None else f"; this one is on {database!r}"
        raise MultiStoreWriteError(
            f"{what} by a second unit of this request (the writer is a unit on "
            f"{self.writer_database!r}{this}): "
            "one writer per request — do every write in the request's write "
            "unit (db() / DbSession), or declare "
            "allow_multi_db_write(secondaries=..., reason=...) for another "
            "database (SANDLOOM-R007)"
        )


#: Attribute holding a unit's admission serial (on the unit object itself:
#: a connection's ``info`` belongs to the pooled DBAPI connection and would
#: survive into another checkout).
_SERIAL_ATTR = "_sandloom_admission_serial"
_serials = itertools.count(1)


def _unit_key(unit: Any) -> int:
    """A unit's identity for admission: a process-unique serial set on the
    unit (never reused, unlike the ``id()`` of a freed unit)."""
    serial = getattr(unit, _SERIAL_ATTR, None)
    if serial is None:
        serial = next(_serials)
        try:
            setattr(unit, _SERIAL_ATTR, serial)
        except AttributeError:  # pragma: no cover - a unit without attributes
            return id(unit)
    return serial


_admission: ContextVar[WriteAdmission | None] = _admission_var
#: Bound by assignment (keeps the ``.get()`` method-call fast path).
admission_get = _admission.get


@contextmanager
def fresh_admission_scope(admission: WriteAdmission | None = None) -> Iterator[None]:
    """Run the enclosed units in their own admission scope (contract, consumer,
    task, saga-step and kernel units): ``None`` = no admission at all."""
    token = _admission.set(admission)
    try:
        yield
    finally:
        _admission.reset(token)


# --------------------------------------------------------------------------- #
# The request scope
# --------------------------------------------------------------------------- #
#: The empty self-revocation set every scope starts with (replaced, never
#: mutated: a committed unit's survivors are merged into a new set).
_NO_SUBJECTS: frozenset[str] = frozenset()


class RequestScope(WriteAdmission):
    """Per-request unit state (installed by the route runtime).

    The scope IS the request's :class:`WriteAdmission` (one object per
    request; ``admission`` returns it) unless an explicit admission is
    passed. Built on every request: :meth:`for_route` is the route runtime's
    allocation-lean constructor.
    """

    __slots__ = (
        "_admission_override",
        "armed",
        "at_commit",
        "check",
        "claims",
        "committed",
        "core",
        "handler_task",
        "intent",
        "lazy",
        "lazy_factory",
        "policy",
        "request",
        "risk",
        "self_revoked",
        "subject",
    )

    def __init__(
        self,
        *,
        core: Any,
        policy: Any,
        request: Any = None,
        admission: WriteAdmission | None = None,
    ) -> None:
        WriteAdmission.__init__(self)
        self.core = core
        self.policy = policy
        self.request = request
        self.handler_task = _task()
        self._admission_override = admission
        #: the lazy request unit (UnitOfWorkContract) once db() created it
        self.lazy: Any = None
        #: ``(core, database) -> unit`` builder of the lazy unit (route runtime)
        self.lazy_factory: Callable[[], Any] | None = None
        #: the commit-time re-check: armed for the request's committing units
        self.armed = False
        #: explicit at_commit(): check before EVERY unit, empty ones included
        self.at_commit = False
        #: the request's authenticated subject (``sub``) and the live
        #: check of its session (set by the route runtime on user routes)
        self.subject: str | None = None
        self.check: Callable[[], Awaitable[None]] | None = None
        self.claims: dict[str, Any] | None = None
        #: subjects self-revoked by units of this request that COMMITTED
        self.self_revoked: frozenset[str] | set[str] = _NO_SUBJECTS
        #: operation-intent bookkeeping (None: not an intent route)
        self.intent: Any = None
        #: risk bookkeeping (``sandloom.core.unit_of_work.risk.RiskState``, lazy)
        self.risk: Any = None
        # ``committed`` (the databases of the writing units that committed,
        # on an ``allow_multi_db_write`` route) is set only by
        # :func:`note_committed` there: unset elsewhere (no cost on the
        # single-store path), read with getattr

    @classmethod
    def for_route(
        cls,
        core: Any,
        policy: Any,
        request: Any,
        write_store: str | None,
        secondaries: frozenset[str],
        sequential: bool,
        lazy_factory: Callable[[], Any] | None,
        task: Any,
    ) -> RequestScope:
        """The route runtime's constructor: one allocation, no keyword
        parsing, the admission's frozen inputs precomputed per route."""
        self = cls.__new__(cls)
        self.write_store = write_store
        self.secondaries = secondaries
        self.sequential = sequential
        self.writer = None
        self.writer_database = None
        self.writers = {}
        self.core = core
        self.policy = policy
        self.request = request
        self.handler_task = task
        self._admission_override = None
        self.lazy = None
        self.lazy_factory = lazy_factory
        self.armed = False
        self.at_commit = False
        self.subject = None
        self.check = None
        self.claims = None
        self.self_revoked = _NO_SUBJECTS
        self.intent = None
        self.risk = None
        return self

    @property
    def admission(self) -> WriteAdmission:
        """The request's write admission: the scope itself (or the explicit
        admission it was built with)."""
        override = self._admission_override
        return self if override is None else override


_request_scope: ContextVar[RequestScope | None] = _request_scope_var
request_scope_get = _request_scope.get


def current_request_scope() -> RequestScope | None:
    """The scope of the request running in this context (``None`` outside)."""
    return _request_scope.get()


@contextmanager
def bind_request_scope(scope: RequestScope | None) -> Iterator[None]:
    """Bind ``scope`` (and its admission) for the enclosed code."""
    token = _request_scope.set(scope)
    adm = _admission.set(scope.admission if scope is not None else None)
    try:
        yield
    finally:
        _admission.reset(adm)
        _request_scope.reset(token)


# --------------------------------------------------------------------------- #
# Unit ownership and db()
# --------------------------------------------------------------------------- #
def claim_unit(session: Any) -> None:
    """Record the current task as ``session``'s owner (unit open)."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        scope = _request_scope.get()
        info[OWNER_KEY] = _task()
        info.pop(CLOSED_KEY, None)
        info.pop(RECHECKED_KEY, None)
        info[SCOPE_KEY] = scope
        if (
            scope is not None
            and scope.intent is not None
            and _admission.get() is scope.admission
        ):
            # one of the intent route's own units (contract units run in a
            # fresh admission scope and are not)
            scope.intent.on_unit_open(session)
            info[INTENT_UNIT_KEY] = True


def claim_owner(session: Any) -> None:
    """Hand an exclusively held unit to the current task (kernel use: the
    contract read scope's shared unit, reused under its busy flag)."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[OWNER_KEY] = _task()


def release_unit(session: Any) -> None:
    """Mark ``session``'s unit closed: any later use is refused (SANDLOOM-R002)."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[CLOSED_KEY] = True
        if info.get(INTENT_UNIT_KEY):
            scope = info.get(SCOPE_KEY)
            if scope is not None and scope.intent is not None:
                scope.intent.on_unit_close(session)


def _refuse_foreign(session: Any) -> None:
    owner = session.info.get(OWNER_KEY)
    if owner is not None and owner is not _task():
        raise ConcurrentSessionUseError(
            "db() from a task that does not own the unit: a child task (asyncio."
            "create_task / gather) inherits its parent's unit but may not use "
            "it — open the child's own unit: `async with db_session() as "
            "session:` (SANDLOOM-R013)"
        )


def db() -> Any:
    """The session of the innermost open unit of the current context.

    Synchronous. Outside any unit it raises :class:`UnitOfWorkRequiredError`
    (SANDLOOM-R002); from a task that does not own the unit
    :class:`ConcurrentSessionUseError` (SANDLOOM-R013). In a request with no open
    unit it creates the lazy request unit (handler task only), committed when
    the handler succeeds and rolled back on any exception, before the
    response is sent. Use ``flush()``, never ``commit()``. Passing the session
    to another task is unsupported.
    """
    session = _current_uow_session.get()
    if session is not None:
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            if info.get(CLOSED_KEY):
                raise UnitOfWorkRequiredError(
                    "db(): the unit has exited (SANDLOOM-R002)"
                )
            _refuse_foreign(session)
        return session
    scope = _request_scope.get()
    if scope is not None:
        return _lazy_unit(scope)
    raise UnitOfWorkRequiredError(
        "db() outside a Unit of Work: call it inside a request handler, a "
        "contract method, an event consumer or an `async with db_session():` "
        "block (SANDLOOM-R002)"
    )


def join_request_scope(session: Any) -> None:
    """A test-owned session stands in for the request unit: it takes the
    request's scope (commit-time re-check, ownership) like a real unit."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        claim_unit(session)
        info.pop(SELF_REVOKED_KEY, None)
        info.pop(SELF_REVOKED_FINAL_KEY, None)
        info.pop(WRITES_KEY, None)


def stash_test_pending_check(core: Any, session: Any) -> None:
    """The test-owned session asks the default database's driver for
    unflushed changes, like a kernel-bound unit."""
    try:
        provider = core.database("default")
    except (AttributeError, LookupError, RuntimeError, TypeError):  # bare facade
        return
    _stash_pending_check(session, provider)


#: Test seam (the harness's ``client`` fixture): builds the request unit in
#: place of the route's lazy unit, so ``db()`` in a request runs on the test's
#: shared session exactly like ``DbSession`` does under the ``get_db``
#: override. ``None`` in production (one global read per request that opens
#: its unit).
request_unit_override: Callable[[RequestScope], Any] | None = None


def _lazy_unit(scope: RequestScope) -> Any:
    if scope.handler_task is not _task():
        raise ConcurrentSessionUseError(
            "db() from a child task before the request opened its unit: nothing "
            "was created — open the child's own unit: `async with db_session() "
            "as session:` (SANDLOOM-R013)"
        )
    intent = scope.intent
    if intent is not None:
        intent.refuse_request_unit()
    factory = scope.lazy_factory
    if factory is None:
        raise UnitOfWorkRequiredError(
            "db() in a request whose route opens no request unit (SANDLOOM-R002)"
        )
    unit = scope.lazy
    if unit is None:
        override = request_unit_override
        unit = scope.lazy = factory() if override is None else override(scope)
    session = unit.bind_lazily()
    return session


def open_request_unit(scope: RequestScope) -> Any:
    """Open the request's lazy unit from the handler task (the route
    runtime: the mandatory audit row of a request whose risky command
    committed in its own unit, G-SENS-04); its session."""
    return _lazy_unit(scope)


def record_self_revocation(session: Any, subject: str, token: Any) -> None:
    """Record that this unit's code self-revoked ``subject`` (after its live
    check passed); tied to the savepoint ``token`` that made the call."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info.setdefault(SELF_REVOKED_KEY, []).append((subject, token))


def note_committed(session: Any) -> None:
    """A unit committed: its surviving self-revocations now count for the
    request's later commits (spec §4.5 item 2/3)."""
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    final = info.pop(SELF_REVOKED_FINAL_KEY, None)
    scope = info.get(SCOPE_KEY)
    if scope is None:
        return
    if final:
        scope.self_revoked = scope.self_revoked | final
    if info.get(INTENT_UNIT_KEY) and scope.intent is not None:
        scope.intent.on_unit_commit(int(info.get(WRITES_KEY, 0)))
    if scope.secondaries and info.get(WRITES_KEY):
        # best-effort 1PC: a later failing commit leaves this store committed
        scope.committed = (*getattr(scope, "committed", ()), info.get(_DB_NAME_KEY))


def unit_database(session: Any) -> str | None:
    info = getattr(session, "info", None)
    return info.get(_DB_NAME_KEY) if isinstance(info, dict) else None


def active_units() -> tuple[Any, ...]:
    """Every unit open in the current context (outermost first)."""
    return _active_uow_sessions.get()


def unit_write_count(session: Any) -> int:
    info = getattr(session, "info", None)
    return int(info.get(WRITES_KEY, 0)) if isinstance(info, dict) else 0


__all__ = [
    "CLOSED_KEY",
    "OWNER_KEY",
    "RECHECKED_KEY",
    "SCOPE_KEY",
    "SELF_REVOKED_FINAL_KEY",
    "SELF_REVOKED_KEY",
    "WRITES_KEY",
    "ConcurrentSessionUseError",
    "MultiStoreWriteError",
    "RequestScope",
    "UnitRuleError",
    "WriteAdmission",
    "active_units",
    "admission_get",
    "bind_request_scope",
    "claim_owner",
    "claim_unit",
    "current_request_scope",
    "db",
    "fresh_admission_scope",
    "note_committed",
    "open_request_unit",
    "record_self_revocation",
    "release_unit",
    "request_scope_get",
    "unit_database",
    "unit_write_count",
]
