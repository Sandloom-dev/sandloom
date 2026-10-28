"""Unit-of-Work scope: which session governs the current request / task.

The context-scoped "current UoW session" bookkeeping (innermost + every active
session), the read-only scope, the ``session.info`` keys the engine stashes on
a UoW session, the outbox / audit store and event-bus resolution, the
request-authority boundary and the request commit checks a UoW captures, and
DATABASE SELECTION — which active session an effect without an explicit
``database=`` lands in (:func:`_resolve_scoped_session`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import (
    contextmanager,
)
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NamedTuple

from sandloom.core.contract_context import _isolated_contract_request_scope
from sandloom.core.infra.contracts import AuditStore, OutboxHandle, OutboxStore
from sandloom.core.tenant_context import (
    _bypass,
    _current_tenant,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Annotation-only: the engine is paradigm-agnostic and imports no SQLAlchemy
    # at runtime (relational specifics stay behind the lazy sql_alchemy imports),
    # so it sits OUTSIDE the relational-paradigm boundary ratchet.
    from sqlalchemy.ext.asyncio import AsyncSession

    from sandloom.core.facade import CoreFacade
import sandloom.core.infra.registry as _registry
import sandloom.core.runtime as _runtime

logger = logging.getLogger("sandloom.core.unit_of_work")


# Session of the UoW governing the current request/task, if any (the INNERMOST
# one when several are nested). record_audit uses it; publish_event uses it only
# as the unambiguous single-database fast path.
_current_uow_session: ContextVar[AsyncSession | None] = ContextVar(
    "sandloom_current_uow_session", default=None
)
# All UoW sessions active in the current context (outermost first). When an
# endpoint holds more than one database session at once (e.g. DbSession +
# Database("analytics")), publish_event(database=...) selects which database's
# transactional outbox the event is written to — each database owns its own
# outbox, so an implicit "current session" would otherwise risk writing the
# event to the wrong database. Ambiguity (>1 distinct database, no explicit
# choice) is rejected rather than guessed.
_active_uow_sessions: ContextVar[tuple[AsyncSession, ...]] = ContextVar(
    "sandloom_active_uow_sessions", default=()
)
# Declared READ-ONLY scope (execution planner, PROTOTYPE): while set, every
# write-path primitive (publish_event, record_audit, on_commit) REFUSES instead
# of falling back to "no UoW" behaviour (a bus publish that bypasses the outbox,
# a silently skipped audit row). The value names who declared the scope.
_read_only_scope: ContextVar[str | None] = ContextVar(
    "sandloom_read_only_scope", default=None
)


class ReadOnlyScopeError(RuntimeError):
    """A write-path primitive was called inside a declared read-only scope."""


@contextmanager
def read_only_scope(owner: str) -> Iterator[None]:
    """Declare the current context read-only (nests; the outermost wins).

    Inside it ``publish_event`` / ``record_audit`` (required or not) /
    ``on_commit`` raise :class:`ReadOnlyScopeError` before touching any store
    or bus — whether or not a Unit of Work is also open.
    """
    token = _read_only_scope.set(_read_only_scope.get() or owner)
    try:
        yield
    finally:
        _read_only_scope.reset(token)


def _refuse_in_read_only_scope(op: str) -> None:
    owner = _read_only_scope.get()
    if owner is not None:
        raise ReadOnlyScopeError(
            f"{op}() called inside a read-only scope declared by {owner}; "
            "declared read operations cannot publish events, write audit rows "
            "or queue post-commit effects — use a read-write route (DbSession)"
        )


# Key in session.info holding the logical database name this UoW governs, so
# publish_event(database=...) can match a session to a requested database.
_DB_NAME_KEY = "sandloom_db_name"

# Key in session.info holding events pending post-commit dispatch.
_OUTBOX_PENDING_KEY = "sandloom_outbox_pending"
# Key in session.info holding the UoW factory that opened the session, so the
# post-commit dispatcher can mark outbox rows delivered in the SAME database
# (critical for multi-database deployments where each DB owns its own outbox).
_UOW_FACTORY_KEY = "sandloom_uow_factory"
# Key in session.info holding the APP-OWNED event bus that governs this scope.
# Bound when the UoW is opened so publish_event preflight and the post-commit
# dispatcher use this application's bus rather than a process-global accessor
# (multi-app safe; see sandloom.core.runtime).
_EVENT_BUS_KEY = "sandloom_event_bus"
# Keys in session.info holding the backend-specific outbox/audit writers for the
# database this UoW governs. Stashed when the UoW opens so publish_event /
# record_audit reach the right backend's store WITHOUT importing a concrete ORM
# model (the relational store is the only thing that knows OutboxMessage).
_OUTBOX_STORE_KEY = "sandloom_outbox_store"
_AUDIT_STORE_KEY = "sandloom_audit_store"
# Sentinel stashed when a provider exposes no outbox/audit store (its driver
# lacks the transactional_outbox / audit feature).
_UNSUPPORTED = object()


def _session_bus(session: AsyncSession) -> Any:
    """Return the app-owned bus bound to ``session``, or the active runtime's.

    Every UoW-opening path stashes ``core.event_bus`` under ``_EVENT_BUS_KEY``.
    Falling back to ``active_event_bus()`` keeps ad-hoc sessions (that never set
    it) working via the active runtime's bus.
    """
    bus = session.info.get(_EVENT_BUS_KEY)
    if bus is not None:
        return bus

    return _runtime.active_event_bus()


class _PendingEvent(NamedTuple):
    # Backend-agnostic handle (envelope id + opaque row handle): the store tells
    # a committed row from one whose INSERT was undone by a rolled-back SAVEPOINT
    # (session.info itself is not savepoint-aware).
    handle: OutboxHandle
    channel: str
    schema_version: str
    payload: dict[str, Any]


def _outbox_store(session: AsyncSession) -> OutboxStore:
    """Return the outbox store governing ``session``.

    A stashed store is used. The ``_UNSUPPORTED`` sentinel means the configured
    driver has no transactional outbox. A missing binding is a wiring error;
    neither case selects an implicit SQLAlchemy database.
    """
    store = session.info.get(_OUTBOX_STORE_KEY)
    if store is _UNSUPPORTED:
        raise _registry.TransactionalOutboxUnsupportedError(
            "publish_event called inside a UoW on a database whose driver has "
            "no transactional outbox",
            fix="use a database driver that advertises the "
            "'transactional_outbox' feature, or do not publish events on this "
            "database",
        )
    if store is not None:
        return store  # type: ignore[no-any-return]
    raise RuntimeError(
        "No outbox store is bound to this UoW session; open the session through "
        "DbSession, Database(name), or db_session(name)"
    )


def _audit_store(session: AsyncSession) -> AuditStore | None:
    """Return the audit store governing ``session``, or ``None`` to skip.

    Like :func:`_outbox_store`, but an explicitly unsupported audit capability
    returns ``None`` so non-required audit remains best-effort. A missing store
    binding is a wiring error.
    """
    store = session.info.get(_AUDIT_STORE_KEY)
    if store is _UNSUPPORTED:
        return None
    if store is not None:
        return store  # type: ignore[no-any-return]
    raise RuntimeError(
        "No audit store is bound to this UoW session; open the session through "
        "DbSession, Database(name), or db_session(name)"
    )


def _stash_stores(session: AsyncSession, core: CoreFacade | None, name: str) -> None:
    """Stash the database's outbox/audit stores on the session.

    Resolves them from the named database provider when an infra registry is
    bound; records the ``_UNSUPPORTED`` sentinel when the provider exposes none
    (so publish_event refuses instead of silently using another store).
    Registry and provider errors propagate.
    """
    if core is None:
        raise RuntimeError("Cannot bind database stores without an active CoreFacade")
    provider = core.database(name)
    outbox = provider.outbox()
    session.info[_OUTBOX_STORE_KEY] = outbox if outbox is not None else _UNSUPPORTED
    audit = provider.audit()
    session.info[_AUDIT_STORE_KEY] = audit if audit is not None else _UNSUPPORTED
    stash_pending_check(session, provider)


#: ``session.info`` key: the database provider's ``has_pending_changes``
#: (a bound method of the PROVIDER — never of the unit — so no cycle).
_PENDING_CHECK_KEY = "sandloom_pending_check"


def stash_pending_check(session: Any, provider: Any) -> None:
    """Record how the commit hooks ask ``session``'s driver for unflushed
    changes (``DatabaseProvider.has_pending_changes``)."""
    info = getattr(session, "info", None)
    check = getattr(provider, "has_pending_changes", None)
    if isinstance(info, dict) and check is not None:
        info[_PENDING_CHECK_KEY] = check


@contextmanager
def _bind_uow_scope(session: AsyncSession, name: str) -> Iterator[None]:
    """Bind ``session`` as an active UoW for database ``name`` in this context.

    Records the logical database name on the session, sets it as the innermost
    current session (for record_audit) and pushes it onto the active-sessions
    stack (so publish_event(database=...) can target the right outbox when more
    than one database is open). Restores both on exit.
    """
    session.info[_DB_NAME_KEY] = name
    cur_token = _current_uow_session.set(session)
    stack_token = _active_uow_sessions.set((*_active_uow_sessions.get(), session))
    try:
        yield
    finally:
        _active_uow_sessions.reset(stack_token)
        _current_uow_session.reset(cur_token)


def uow_database_name(session: Any) -> str | None:
    """The logical database a framework UoW session governs (``None``: the
    session was not opened by a Unit of Work, e.g. a caller-owned one)."""
    info = getattr(session, "info", None)
    return info.get(_DB_NAME_KEY) if isinstance(info, dict) else None


def is_active_uow_session(session: Any) -> bool:
    """True while ``session`` is one of the current context's open UoWs."""
    return session in _active_uow_sessions.get()


def _resolve_scoped_session(database: str | None, *, op: str) -> AsyncSession | None:
    """Pick the UoW session a scoped write (publish_event / record_audit) targets.

    * ``database`` given — the active session governing exactly that database
      (innermost if nested); raise if none is open in this scope (writing to a
      database whose UoW is not open here is a programming error, not a silent
      non-transactional write).
    * ``database`` omitted — the single active session; raise if more than one
      *distinct* database is active (ambiguous — the caller must disambiguate);
      ``None`` if no UoW is active.

    ``op`` is the caller name used in the error messages.
    """
    active = _active_uow_sessions.get()
    current = _current_uow_session.get()
    if database is not None:
        for sess in reversed(active):
            if sess.info.get(_DB_NAME_KEY) == database:
                return sess
        if current is not None and current.info.get(_DB_NAME_KEY) == database:
            return current
        raise RuntimeError(
            f"{op}(database={database!r}) but no open Unit of Work session for "
            "that database in the current scope; open it (e.g. via "
            f"Database({database!r}) or db_session({database!r})) first"
        )
    # No explicit database: only the stack disambiguates. More than one DISTINCT
    # database active at once is ambiguous — refuse rather than guess.
    if len(active) > 1:
        names = {sess.info.get(_DB_NAME_KEY, "default") for sess in active}
        if len(names) > 1:
            raise RuntimeError(
                f"{op}() is ambiguous: multiple databases are active in this "
                f"scope ({sorted(names)}); pass database=<name> to choose which "
                "database the write targets"
            )
    # Innermost current session (== the single active one in normal flow); also
    # the path for callers that bind _current_uow_session directly (tests).
    return current


OnCommitCallback = Callable[[], Any]

#: The request scope (:mod:`sandloom.core.unit_of_work.request`) of the request
#: running in this context, and its write admission (a contract / consumer /
#: kernel unit may run in a fresh admission scope inside a request).
_request_scope: ContextVar[Any] = ContextVar("sandloom_request_scope", default=None)
_admission: ContextVar[Any] = ContextVar("sandloom_write_admission", default=None)


@contextmanager
def _isolated_request_authority_scope() -> Iterator[None]:
    """A new HTTP request never shares its caller's transaction/authority scope.

    This also applies to nested in-process ASGI calls. Local contract calls do
    not enter this boundary and continue inheriting the request's scope (its
    commit-time re-check reaches their command units).
    """
    request = _request_scope.set(None)
    admission = _admission.set(None)
    current = _current_uow_session.set(None)
    active = _active_uow_sessions.set(())
    readonly = _read_only_scope.set(None)
    tenant = _current_tenant.set(None)
    bypass = _bypass.set(False)
    try:
        with _isolated_contract_request_scope():
            yield
    finally:
        _bypass.reset(bypass)
        _current_tenant.reset(tenant)
        _read_only_scope.reset(readonly)
        _active_uow_sessions.reset(active)
        _current_uow_session.reset(current)
        _admission.reset(admission)
        _request_scope.reset(request)
