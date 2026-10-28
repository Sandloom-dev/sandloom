"""Test-harness helpers: run code under a Unit-of-Work scope without a request.

Module tests usually override the raw DB provider (the test owns the session
and its transaction), which bypasses the request UoW — so ``record_audit`` /
``publish_event`` find no scope. Security audits are ``required=True`` (a
missing UoW is an error, never a silently dropped audit row), so tests need a
way to bind one:

* :func:`bind_test_uow` — bind a REAL session (the test's ``AsyncSession``) as
  the current UoW for ``database``, with the relational outbox/audit stores:
  audit rows and outbox rows are written into the test's own transaction.
  Typical use is inside a ``get_db`` override::

      async def override_get_db():
          with bind_test_uow(test_db):
              yield test_db

* :func:`recording_uow` — bind an in-memory stand-in session whose stores only
  RECORD what was written (``.audits`` / ``.events``), for unit tests that run
  services against a mocked session.
* :func:`capture_effects` — record the exact ``publish_event`` /
  ``record_audit`` calls a module's services make (patched at the module seam),
  for services running on a session that is not bound as the UoW.
* :func:`committing_uow` — a REAL committing UoW on a session factory (commit
  on success, rollback on error, then the ``on_commit`` callbacks), for tests
  that must observe what happens only after — or never after — a commit.

Never use these in application code: they skip the commit/rollback and the
post-commit dispatch a real UoW performs (events stay in the outbox / record).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from uuid import uuid4

import sandloom.core.unit_of_work as _unit_of_work
import sandloom.core.unit_of_work.request as _request
from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.contracts import (
    AuditStore,
    CacheProvider,
    OutboxHandle,
    OutboxStore,
    parse_plain_decimal,
)
from sandloom.core.module_api import bind_contract_session
from sandloom.core.sql_alchemy.outbox_store import (
    sql_alchemy_audit_store,
    sql_alchemy_outbox_store,
)
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tenant_context import configured_tenant_scope
from sandloom.core.unit_of_work.hooks import _run_before_commit_callbacks
from sandloom.core.unit_of_work.scope import (
    _AUDIT_STORE_KEY,
    _OUTBOX_STORE_KEY,
    _bind_uow_scope,
    _resolve_scoped_session,
)


@contextmanager
def bind_test_uow(
    session: Any,
    *,
    database: str = "default",
    outbox: OutboxStore | None = None,
    audit: AuditStore | None = None,
) -> Iterator[Any]:
    """Bind ``session`` as the active UoW for ``database`` in this context.

    Uses the relational (SQLAlchemy) outbox/audit stores unless explicit ones
    are given. The caller owns the transaction (commit/rollback as the test
    needs); nothing is dispatched to the event bus.
    """
    if outbox is None or audit is None:
        outbox = outbox or sql_alchemy_outbox_store()
        audit = audit or sql_alchemy_audit_store()
    session.info[_OUTBOX_STORE_KEY] = outbox
    session.info[_AUDIT_STORE_KEY] = audit
    with _bind_uow_scope(session, database):
        yield session


@dataclass
class RecordingSession:
    """Stand-in UoW session for :func:`recording_uow` (only ``info``)."""

    info: dict[str, Any] = field(default_factory=dict)
    audits: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)

    def audit_actions(self) -> list[str]:
        return [entry["action"] for entry in self.audits]


class _RecordingAuditStore(AuditStore):
    def record(self, session: Any, **fields: Any) -> None:
        session.audits.append(fields)


class _RecordingOutboxStore(OutboxStore):
    """Records enqueued events; the relay side is unsupported (never relayed)."""

    def enqueue(
        self,
        session: Any,
        channel: str,
        payload: Mapping[str, Any],
        schema_version: str,
    ) -> OutboxHandle:
        session.events.append((channel, dict(payload), schema_version))
        return OutboxHandle(id=uuid4())

    async def mark_delivered(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("recording outbox is never relayed")

    async def fetch_pending_ids(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("recording outbox is never relayed")

    async def deliver_pending(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("recording outbox is never relayed")


@contextmanager
def recording_uow(*, database: str = "default") -> Iterator[RecordingSession]:
    """Bind an in-memory UoW that records audits/events (unit tests)."""
    session = RecordingSession()
    with bind_test_uow(
        session,
        database=database,
        outbox=_RecordingOutboxStore(),
        audit=_RecordingAuditStore(),
    ):
        yield session


async def run_on_commit(session: Any = None, *, database: str | None = None) -> None:
    """Treat a test-owned UoW session as committed: run its ``before_commit``
    checks (a failing one propagates and no ``on_commit`` callback runs —
    like a real UoW that refuses to commit), then its ``on_commit`` callbacks.

    :func:`bind_test_uow` / :func:`recording_uow` never commit (the test owns
    the transaction), so post-commit side effects (e.g. ``revoke_user_tokens``
    queued via ``on_commit``) stay pending. Call this where the test treats the
    work as committed — e.g. after the ``yield`` of a ``get_db`` override, which
    is skipped when the endpoint raised (the request "rolled back")::

        async def override_get_db():
            with bind_test_uow(test_db):
                yield test_db
            await run_on_commit(test_db)

    Without ``session`` it takes the UoW currently bound for ``database``
    (``bind_test_uow`` / ``recording_uow`` / an ambient test UoW); with none
    bound there is nothing pending — ``on_commit`` outside a UoW runs at once.
    """
    if session is None:
        session = _resolve_scoped_session(database, op="run_on_commit")
        if session is None:
            return
    # The commit pipeline (unit_of_work.COMMIT_PIPELINE) minus the outbox
    # stage: the test owns the transaction, its outbox rows never commit.
    await _run_before_commit_callbacks(session)
    await _unit_of_work._dispatch_outbox_after_commit(session, dispatch=False)


@asynccontextmanager
async def committing_uow(
    session_factory: Any, *, database: str = "default"
) -> AsyncIterator[Any]:
    """Run the block in a REAL Unit of Work that commits, like ``db_session()``.

    ``session_factory`` is an ``async_sessionmaker`` on the test engine (e.g.
    ``async_sessionmaker(db_engine, expire_on_commit=False)``). The session is
    bound as the UoW (outbox / audit rows join its transaction); on a clean
    exit it COMMITS and then runs the queued ``on_commit`` callbacks; an
    exception rolls back and skips them — the request / worker sequence,
    without the post-commit bus dispatch::

        with pytest.raises(Boom):
            async with committing_uow(sessions) as db:
                await Service.remove(..., db)
                raise Boom          # rolled back: no post-commit effect ran
    """
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        session = uow.session
        if session is None:  # pragma: no cover - defensive
            raise RuntimeError("UnitOfWork did not provide a session")
        with bind_test_uow(session, database=database):
            yield session
    await run_on_commit(session)  # reached only after a successful commit


#: :func:`capture_effects` result: ``{"events": [(channel, payload)],
#: "audit": [(action, kwargs)]}``.
Captured = dict[str, list[Any]]


def capture_effects(monkeypatch: Any, *modules: ModuleType) -> Captured:
    """Record ``publish_event`` / ``record_audit`` and typed ``Event.publish()``
    calls made from ``modules``.

    Patches the two names in each module (where the service imported them
    from ``sandloom.core.di``), so the calls never reach a UoW: events are recorded
    as ``(channel, payload)`` and audits as ``(action, kwargs)`` — the exact
    keyword arguments, so ``kwargs["required"]`` can be asserted. Use it when
    the service runs on a session that is not bound as the UoW (committed
    sessions of a race test, a direct service call).
    """
    calls: Captured = {"events": [], "audit": []}

    async def fake_publish(
        channel: str, payload: dict[str, Any], *args: Any, **kwargs: Any
    ) -> None:
        calls["events"].append((channel, payload))

    def fake_audit(action: str, **kwargs: Any) -> None:
        calls["audit"].append((action, kwargs))

    for module in modules:
        # A module that publishes only typed events (``Event.publish()``) or
        # audits nothing does not import the names: nothing to patch there.
        if hasattr(module, "publish_event"):
            monkeypatch.setattr(module, "publish_event", fake_publish)
        if hasattr(module, "record_audit"):
            monkeypatch.setattr(module, "record_audit", fake_audit)
    _capture_typed_events(monkeypatch, modules, calls)
    return calls


#: Where ``Event.publish()`` (Public API 1.0, ``sandloom.sdk``) resolves
#: ``publish_event``. Looked up in ``sys.modules`` only: the kernel never
#: imports ``sandloom.sdk``, and no typed event can be published before it loads.
_TYPED_EVENTS_MODULE = "sandloom.sdk._decl.events"


def _capture_typed_events(
    monkeypatch: Any, modules: tuple[ModuleType, ...], calls: Captured
) -> None:
    """Record ``await SomeEvent(...).publish()`` made from ``modules`` too.

    A typed event publishes through the sdk's ``publish_event``; the patch
    records the call when the code calling ``.publish()`` lives in one of
    ``modules`` and forwards any other call unchanged.
    """
    typed = sys.modules.get(_TYPED_EVENTS_MODULE)
    if typed is None or not modules:
        return
    original = typed.publish_event
    names = {module.__name__ for module in modules}

    async def fake_typed_publish(
        channel: str, payload: dict[str, Any], *args: Any, **kwargs: Any
    ) -> None:
        # frame 0: this function, 1: Event.publish, 2: the caller of .publish()
        caller = sys._getframe(2).f_globals.get("__name__")
        if caller in names:
            calls["events"].append((channel, payload))
            return
        await original(channel, payload, *args, **kwargs)

    monkeypatch.setattr(typed, "publish_event", fake_typed_publish)


class InMemoryCache(CacheProvider):
    """Dict-backed :class:`CacheProvider` (incl. ``cas`` and an atomic
    ``set_max``) for unit tests.

    TTLs are accepted and ignored. ``broken=True`` makes every operation raise
    ``ConnectionError`` (a cache outage). Install it where the code under test
    resolves the cache, e.g. ``monkeypatch.setattr(cache_store, "cache",
    lambda name="default": mem)`` (``sandloom.core.idempotency.cache_store``) for
    the idempotency / contract-dedupe store.
    """

    driver = "memory"

    def features(self) -> frozenset[str]:
        return frozenset({"get", "set", "add", "delete", "incr", "cas"})

    def __init__(self, *, broken: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.broken = broken

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("cache down")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.store.get(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self._check()
        self.store[key] = value

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._check()
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self._check()
        self.store.pop(key, None)

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self._check()
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        self._check()
        if self.store.get(key) != expected:
            return False
        self.store[key] = value
        return True

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        self._check()
        if self.store.get(key) != expected:
            return False
        del self.store[key]
        return True

    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        """Atomic monotonic write.

        Reads through :meth:`get` first, so a subclass's fault injection
        (an outage simulated in ``get``) applies, then decides and writes on
        the store with no await in between — atomic even when ``get`` yields.
        """
        self._check()
        ours = parse_plain_decimal(value)
        if ours is None:
            raise ValueError(
                f"set_max value must be a plain non-negative decimal: {value!r}"
            )
        await self.get(key)
        current = self.store.get(key)
        existing = parse_plain_decimal(current)
        if current is not None and existing is not None and existing >= ours:
            return current
        self.store[key] = value
        return value

    async def health(self) -> HealthResult:
        return HealthResult(status="fail" if self.broken else "ok")


# --------------------------------------------------------------------------- #
# The ``client`` fixture's request unit
# --------------------------------------------------------------------------- #


class SharedRequestUnit:
    """The request unit of a ``client`` request: the test's session."""

    __slots__ = ("_core", "_stack", "session")

    def __init__(self, core: Any, session: Any) -> None:
        self._core = core
        self.session = session
        self._stack: ExitStack | None = None

    def bind_lazily(self) -> Any:
        """The test session, bound as the request's unit (as ``get_db``'s
        override path binds it)."""
        if self._stack is None:
            session = self.session
            stack = ExitStack()
            stack.enter_context(
                configured_tenant_scope(getattr(self._core, "settings", None))
            )
            stack.enter_context(bind_test_uow(session))
            _request.join_request_scope(session)
            _request.stash_test_pending_check(self._core, session)
            stack.enter_context(bind_contract_session(session))
            self._stack = stack
        return self.session

    async def finish(self, exc: BaseException | None) -> None:
        """Success: the before-commit checks, then the ``on_commit``
        callbacks (the outbox rows never commit). Failure: nothing runs and
        nothing is rolled back (shared-session semantics)."""
        stack, self._stack = self._stack, None
        if stack is None:
            return
        try:
            if exc is None:
                await _run_before_commit_callbacks(self.session)
                await _unit_of_work._dispatch_outbox_after_commit(
                    self.session, dispatch=False
                )
        finally:
            try:
                stack.close()
            except ValueError:  # bound in another context: it ends with it
                pass


@contextmanager
def shared_request_unit(core: Any, session: Any) -> Iterator[None]:
    """``db()`` in a request runs on ``session`` for the block."""
    previous = _request.request_unit_override

    def factory(_scope: Any) -> SharedRequestUnit:
        return SharedRequestUnit(core, session)

    _request.request_unit_override = factory
    try:
        yield
    finally:
        _request.request_unit_override = previous


__all__ = [
    "Captured",
    "SharedRequestUnit",
    "InMemoryCache",
    "RecordingSession",
    "bind_test_uow",
    "capture_effects",
    "committing_uow",
    "recording_uow",
    "run_on_commit",
    "shared_request_unit",
]
