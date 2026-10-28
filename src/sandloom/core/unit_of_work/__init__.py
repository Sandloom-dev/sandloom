"""Unit-of-Work runtime engine (the write-path core, paradigm-agnostic seam).

This is the leaf that owns the request/task transaction machinery: the
context-scoped "current UoW session" bookkeeping, the transactional-outbox /
audit store resolution, the post-commit dispatch, and the three public
write-path primitives — :func:`publish_event`, :func:`record_audit`,
:func:`db_session` — plus :func:`durable_inbox`.

It deliberately imports **nothing** from :mod:`sandloom.core.di` or
:mod:`sandloom.core.facade` at runtime, so low-level kernel modules (the event-bus
consumer runner, the projection consumer runner, the RPC dispatcher) can open a
UoW session by importing :func:`db_session` from HERE — downward — instead of
reaching UP into the public DI aggregator. ``sandloom.core.di`` re-exports these
names so business modules keep importing them from ``sandloom.core.di`` (the stable
public surface); the FastAPI request adapters (``get_db`` / ``Database``) stay
in ``di`` and call the engine helpers exposed here.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
)
from typing import TYPE_CHECKING, Any

from prometheus_client import Counter

from sandloom.core.contracts.uow import LazyUnitOfWorkContract, UnitOfWorkContract
from sandloom.core.tenant_context import (
    configured_tenant_scope,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import TracebackType

    # Annotation-only: the engine is paradigm-agnostic and imports no SQLAlchemy
    # at runtime (relational specifics stay behind the lazy sql_alchemy imports),
    # so it sits OUTSIDE the relational-paradigm boundary ratchet.
    from sqlalchemy.ext.asyncio import AsyncSession
import sandloom.core.runtime as _runtime
import sandloom.core.unit_of_work.dispatch as _dispatch
from sandloom.core.unit_of_work.dispatch import _METRIC_POST_COMMIT_DISPATCH_FAILED
from sandloom.core.unit_of_work.hooks import (
    _run_before_commit_callbacks,
    _run_on_commit_callbacks,
)
from sandloom.core.unit_of_work.request import (
    SCOPE_KEY,
    claim_unit,
    note_committed,
)
from sandloom.core.unit_of_work.scope import (
    _DB_NAME_KEY,
    _EVENT_BUS_KEY,
    _UOW_FACTORY_KEY,
    _active_uow_sessions,
    _bind_uow_scope,
    _current_uow_session,
    _stash_stores,
)

logger = logging.getLogger("sandloom.core.unit_of_work")

#: Best-effort 1PC (``allow_multi_db_write``, write-admission rule 4): a unit
#: of the request committed, then a later unit's commit failed. ``route`` is
#: the route template and ``store`` a configured database name (bounded).
_METRIC_PARTIAL_COMMIT = Counter(
    "sandloom_multi_store_partial_commit_total",
    "Requests of an allow_multi_db_write route left partially committed: a "
    "store committed, then a later unit's commit failed (converged by the "
    "client's keyed retry)",
    ["route", "store"],
)

#: ADR-0005 §3 — the stages EVERY Unit of Work ends through, in order:
#:
#: 1. ``before_commit`` — :func:`_run_before_commit_callbacks`, run by the
#:    :class:`~sandloom.core.contracts.uow.UnitOfWorkContract` right before its
#:    ``COMMIT``: the queued checks (minus those of a rolled-back savepoint)
#:    and the request's captured commit checks, each under its
#:    registration-time tenant; the first failure rolls the UoW back.
#: 2. ``COMMIT`` — owned by the ``UnitOfWorkContract``; nothing else commits.
#: 3. ``outbox dispatch`` — :func:`_dispatch_outbox_after_commit`: in line
#:    (``wait=True``), to the background coalescer (``wait=False``) or skipped
#:    (``dispatch=False``: a test-owned transaction whose outbox rows never
#:    commit). A failure is logged + counted and left to the relay.
#: 4. ``on_commit`` — the queued callbacks (same function, always after
#:    stage 3), each under its registration-time tenant; failures are logged.
#:
#: Commit paths: :func:`open_uow` (request ``get_db``, ``Database(name)``,
#: ``db_session()``, ``consumer_db_session()``), the contract runtime's own
#: UoW, and the test seams standing in for a commit (the ``get_db`` override
#: path, :func:`sandloom.core.testing.run_on_commit`). No other code reaches the
#: individual stages (``tests/architecture/test_commit_pipeline.py``).
COMMIT_PIPELINE: tuple[str, ...] = (
    "before_commit",
    "COMMIT",
    "outbox dispatch",
    "on_commit",
)


async def _dispatch_outbox_after_commit(
    session: AsyncSession, *, wait: bool = False, dispatch: bool = True
) -> None:
    """Stages 3–4 of :data:`COMMIT_PIPELINE`: dispatch the committed outbox
    events, then run the ``on_commit`` callbacks.

    Called on EVERY successful UoW commit path (request, ``db_session()``,
    consumer, contract runtime) and never after a rollback. ``dispatch=False``
    skips stage 3 only — for the test seams whose transaction the test owns
    (its outbox rows never commit, so there is nothing to publish).

    ``wait=False`` (the request path: ``DbSession`` / ``Database(name)`` /
    contract calls) hands the committed events to the per-process
    :class:`PostCommitDispatcher` and returns at once — the HTTP response no
    longer waits for broker I/O nor for a second database checkout to mark the
    rows delivered. ``wait=True`` (``db_session()``: workers, scripts,
    consumers) publishes and marks in line before returning, so a short-lived
    loop (Celery's ``run_async``) never leaves the work behind.

    The transaction already committed, so this NEVER raises an ``Exception``:
    a dispatch failure is logged and counted
    (``outbox_post_commit_dispatch_failures_total``) and the committed rows
    are left to the outbox relay — failing here would answer an error for a
    durable change and invite a client retry that re-runs the effect.
    Cancellation (``BaseException``) still propagates.
    """
    note_committed(session)
    if not dispatch:
        await _run_on_commit_callbacks(session)
        return
    try:
        await _dispatch._dispatch_pending_events(session, wait=wait)
    except Exception:  # the transaction already committed: relay is the backstop
        _METRIC_POST_COMMIT_DISPATCH_FAILED.inc()
        logger.error(
            "post-commit outbox dispatch failed after commit; the outbox relay "
            "will deliver the committed events",
            exc_info=True,
        )
    finally:
        await _run_on_commit_callbacks(session)


def _note_commit_failed(session: Any) -> None:
    """A unit's commit (its ``before_commit`` checks or ``COMMIT``) failed.

    When another writing unit of the same ``allow_multi_db_write`` request
    already committed, the request is partially committed (write-admission
    rule 4): count it per committed store in
    ``sandloom_multi_store_partial_commit_total{route,store}`` and log a warning
    naming the route, the committed stores and the failing store. Runs only
    on a failed commit; never raises.
    """
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    scope = info.get(SCOPE_KEY)
    if scope is None:
        return
    committed = getattr(scope, "committed", None)
    if not committed:
        return
    # reported once: a later commit and failure of the request report anew
    scope.committed = ()
    route = str(getattr(scope.policy, "path", None) or "?")
    stores = [str(store) for store in dict.fromkeys(committed)]
    failing = str(info.get(_DB_NAME_KEY))
    for store in stores:
        _METRIC_PARTIAL_COMMIT.labels(route=route, store=store).inc()
    logger.warning(
        "partial commit on %s: %s committed, then the commit of %r failed; a "
        "client retry with the same Idempotency-Key converges it (best-effort "
        "1PC, allow_multi_db_write)",
        route,
        ", ".join(repr(s) for s in stores),
        failing,
        extra={
            "event": "uow.partial_commit",
            "route": route,
            "committed_stores": stores,
            "failing_store": failing,
        },
    )


def _active_core_for(name: str) -> tuple[Any, Any]:
    """``(core, bus)`` of the active runtime for a non-request UoW on ``name``."""

    core = _runtime.get_active_core()
    if core is None:
        raise RuntimeError(
            f"No active CoreFacade for database {name!r}: bind a core via "
            "set_active_core() with the infrastructure registry configured"
        )
    return core, _runtime.active_event_bus()


def _bind_uow_session(
    session: AsyncSession | None,
    core: Any,
    factory: Callable[[], UnitOfWorkContract],
    name: str,
    bus: Any = None,
) -> AsyncSession:
    """Stash what the UoW machinery reads from ``session.info``.

    The factory that opened it (the post-commit dispatcher marks rows
    delivered in the SAME database), the app-owned event bus and the
    database's outbox/audit stores. Every UoW-opening path — :func:`open_uow`
    and the contract runtime — binds a session through here.
    """
    if session is None:  # pragma: no cover - defensive
        raise RuntimeError("UnitOfWork did not provide a session")
    info = session.info
    info[_UOW_FACTORY_KEY] = factory
    info[_EVENT_BUS_KEY] = core.event_bus if bus is None else bus
    _stash_stores(session, core, name)
    claim_unit(session)
    return session


def open_uow(
    core: Any,
    name: str = "default",
    *,
    factory: Callable[[], UnitOfWorkContract] | None = None,
    bus: Any = None,
    wait: bool = False,
) -> AbstractAsyncContextManager[AsyncSession]:
    """Open a Unit of Work on database ``name`` of ``core`` (the one bootstrap).

    One transaction: commit when the block succeeds (after its
    ``before_commit`` checks), rollback on any exception. Inside the block the
    session is bound as the active UoW of ``name`` (``publish_event`` /
    ``record_audit`` / ``on_commit`` target it) and the configured
    single-tenant scope is bound. After a commit — never after a rollback —
    the committed outbox events are dispatched and the ``on_commit`` queue
    runs, outside the block's UoW and tenant scope.

    ``factory`` overrides ``core.get_uow_factory(name)`` (e.g. the read-only
    request factory); ``bus`` overrides ``core.event_bus`` (non-request UoWs
    pass ``runtime.active_event_bus()``). ``wait`` picks the post-commit
    dispatch: ``False`` hands the events to the app loop's background
    coalescer (request and consumer UoWs), ``True`` publishes and marks them
    in line before returning (code on a loop that may not outlive the block:
    Celery tasks, scripts).
    """
    return _OpenUow(core, name, factory, bus, wait)


class _OpenUow:
    """The :func:`open_uow` context manager.

    Behaves exactly like::
        async with uow:
            session = _bind_uow_session(...)
            with _bind_uow_scope(session, name), configured_tenant_scope(...):
                yield session
        await _dispatch_outbox_after_commit(session, wait=wait)

    written out as a class: it runs on every request and every consumer
    delivery, and a generator-based context manager would add a frame and
    a coroutine round trip to each.
    """

    __slots__ = (
        "_bus",
        "_core",
        "_factory",
        "_name",
        "_scope",
        "_session",
        "_tenant",
        "_uow",
        "_wait",
    )

    def __init__(
        self,
        core: Any,
        name: str,
        factory: Callable[[], UnitOfWorkContract] | None,
        bus: Any,
        wait: bool,
    ) -> None:
        self._core = core
        self._name = name
        self._factory = factory
        self._bus = bus
        self._wait = wait

    async def __aenter__(self) -> AsyncSession:
        core, name, factory = self._core, self._name, self._factory
        if factory is None:
            factory = core.get_uow_factory(name)
        uow = self._uow = factory()
        await type(uow).__aenter__(uow)
        try:
            session = _bind_uow_session(uow.session, core, factory, name, self._bus)
            scope = self._scope = _bind_uow_scope(session, name)
            scope.__enter__()
            try:
                tenant = configured_tenant_scope(getattr(core, "settings", None))
                tenant.__enter__()
            except BaseException:
                scope.__exit__(*sys.exc_info())
                raise
        except BaseException:
            await type(uow).__aexit__(uow, *sys.exc_info())
            raise
        self._tenant = tenant
        self._session = session
        return session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        # The scopes exit innermost first, like a ``with`` statement (both
        # only reset context variables: they never suppress); an exception
        # raised by an exit replaces the one in flight.
        uow = self._uow
        try:
            try:
                self._tenant.__exit__(exc_type, exc, tb)
            finally:
                self._scope.__exit__(exc_type, exc, tb)
        except BaseException as err:
            await type(uow).__aexit__(uow, type(err), err, err.__traceback__)
            raise
        try:
            suppressed = await type(uow).__aexit__(uow, exc_type, exc, tb)
        except BaseException:
            if exc is None:  # the commit failed (before_commit or COMMIT)
                _note_commit_failed(self._session)
            raise
        if exc is not None and not suppressed:
            return False
        # Reached only when the UoW committed successfully.
        await _dispatch_outbox_after_commit(self._session, wait=self._wait)
        return exc is not None


class LazyUnit:
    """The lazy request unit (spec §4.3): created by the first ``db()`` of a
    request with no open unit, committed or rolled back by the route runtime
    before the response is sent.

    Construction does no I/O: the session autobegins (and checks a connection
    out) at its first statement. The handler task owns it.
    """

    __slots__ = (
        "_core",
        "_factory",
        "_name",
        "_tokens",
        "_uow",
        "session",
    )

    def __init__(self, core: Any, name: str = "default") -> None:
        self._core = core
        self._name = name
        self._factory: Callable[[], UnitOfWorkContract] | None = None
        self._uow: Any = None
        self._tokens: tuple[Any, ...] | None = None
        self.session: Any = None

    def bind_lazily(self) -> AsyncSession:
        """The unit's session, bound as the innermost unit of this context."""
        session = self.session
        if session is None:
            factory = self._factory = self._core.get_uow_factory(self._name)
            uow = self._uow = factory()
            if not isinstance(uow, LazyUnitOfWorkContract):
                raise RuntimeError(
                    f"database {self._name!r}: its Unit of Work cannot open "
                    "lazily (LazyUnitOfWorkContract: open_lazily / close); "
                    "declare DbSession on the route"
                )
            raw = uow.open_lazily()
            session = self.session = _bind_uow_session(
                raw, self._core, factory, self._name
            )
            session.info[_DB_NAME_KEY] = self._name
        if _current_uow_session.get() is not session:
            # The handler's context: innermost unit for the rest of the request.
            self._tokens = (
                _current_uow_session.set(session),
                _active_uow_sessions.set((*_active_uow_sessions.get(), session)),
            )
        return session  # type: ignore[no-any-return]

    async def finish(self, exc: BaseException | None) -> None:
        """Commit (no exception) or roll back, then close; commit runs the
        full pipeline (before_commit, COMMIT, outbox dispatch, on_commit)."""
        session, uow = self.session, self._uow
        if session is None:
            return
        tokens, self._tokens = self._tokens, None
        if tokens is not None:
            cur, active = tokens
            try:
                _active_uow_sessions.reset(active)
                _current_uow_session.reset(cur)
            except ValueError:  # set in another context: leave it to the boundary
                pass
        committed = False
        try:
            if exc is None:
                try:
                    await _run_before_commit_callbacks(session)
                except BaseException:
                    _note_commit_failed(session)
                    await uow.rollback()
                    raise
                try:
                    await uow.commit()
                except BaseException:
                    _note_commit_failed(session)
                    raise
                committed = True
            else:
                await uow.rollback()
        finally:
            await uow.close()
        if committed:
            await _dispatch_outbox_after_commit(session, wait=False)


@asynccontextmanager
async def db_session(
    name: str = "default",
) -> AsyncGenerator[AsyncSession, None]:
    """UoW-managed session for non-request contexts (Celery tasks, scripts).

    Same semantics as the request path: one transaction, commit on success,
    rollback on exception, transactional outbox for published events.

    ``name`` selects the database instance (default: ``"default"``). A worker
    that operates a non-default database (e.g. ``"analytics"``) passes its
    logical name; the factory is resolved through the active core's
    infrastructure registry, so events publish to THAT database's outbox.

    The committed events are published and marked delivered in line before
    the block exits, so a short-lived loop (a Celery task body under
    ``run_async``, ``asyncio.run`` in a script) never leaves that work behind.
    Code on the app loop (event consumers) uses :func:`consumer_db_session`.
    """
    core, bus = _active_core_for(name)
    async with open_uow(core, name, bus=bus, wait=True) as session:
        yield session


def consumer_db_session(
    name: str = "default",
) -> AbstractAsyncContextManager[AsyncSession]:
    """The delivery UoW of an event consumer (the runtime's session opener).

    :func:`db_session`, except that a handler that publishes does not wait
    for its events' broker round trip and mark-delivered UPDATE before its
    delivery is acked: they go to the app loop's post-commit coalescer, like
    the request path's (and batch with its marks). Only for code on a loop
    that outlives the block. The events are durable before the ack — their
    outbox rows committed with the handler's UoW — and the relay delivers any
    the coalescer does not.

    Returns :func:`open_uow`'s context manager itself (no wrapping layer:
    this runs once per delivery).
    """
    core, bus = _active_core_for(name)
    return open_uow(core, name, bus=bus, wait=False)


__all__ = [
    "COMMIT_PIPELINE",
    "consumer_db_session",
    "db_session",
]
