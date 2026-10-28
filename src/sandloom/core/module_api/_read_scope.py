"""Request-scoped read scope: the shared read connection and the memo of
LOCAL read-contract calls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import sandloom.core.module_api._context as _context
import sandloom.core.tenant_context as _tenant_context
import sandloom.core.unit_of_work as _unit_of_work
import sandloom.core.unit_of_work.request as _request
from sandloom.core import contract_wire
from sandloom.core.config.settings import settings
from sandloom.core.module_api._bootstrap import (
    _contract_uow_factory,
    _isolated_uow_scope,
)
from sandloom.core.module_api._errors import (
    _ReadOnlyRollback,
    read_contract_write_error,
)
from sandloom.core.module_api._read_guard import (
    _guard_read_contract,
    _read_contract_writes,
)

# Hot receivers are bound by ASSIGNMENT, not by ``from ... import``: CPython
# (3.12+) compiles ``name.method()`` on an imported name without the
# method-call fast path (it assumes a module), so ``_var.get()`` would build a
# bound method on every call.
_contract_session_var = _context._contract_session_var
_read_scope_var = _context._read_scope_var

# --------------------------------------------------------------------------- #
# Request-scoped read scope (shared read connection + memo for LOCAL reads)
# --------------------------------------------------------------------------- #


#: Shared empty units map of a scope that never opened one (never mutated:
#: ``run`` replaces it by the scope's own dict first).
_NO_UNITS: dict[Any, Any] = {}
#: ``ContractReadScope.run`` result meaning "not served by the scope".
_SCOPE_FALLBACK: Any = object()
#: Memo miss marker.
_MEMO_MISS: Any = object()

_scope_logger = logging.getLogger("sandloom.core.contract_read_scope")


class _ScopedUnit:
    """One database's shared read Unit of Work inside a read scope."""

    __slots__ = ("unit", "session", "busy", "timer", "idle")

    def __init__(self) -> None:
        self.unit: Any = None
        self.session: Any = None
        self.busy = True  # reserved while it opens
        self.timer: Any = None
        #: set when the running read ends (created only by a waiting drain)
        self.idle: asyncio.Event | None = None

    def done(self) -> None:
        """The read using it ended (or its open failed): wake a drain."""
        self.busy = False
        idle = self.idle
        if idle is not None:
            idle.set()

    async def wait_done(self) -> None:
        if not self.busy:
            return
        idle = self.idle
        if idle is None:
            idle = self.idle = asyncio.Event()
        await idle.wait()


class ContractReadScope:
    """A request-scoped scope in which LOCAL read-contract calls share work.

    Opened per HTTP request by the framework (:class:`ContractReadScopeMiddleware`)
    or explicitly with :func:`contract_read_scope`. Inside it:

    * all local read-contract calls on the same database reuse ONE connection
      / transaction from the provider's contract pool (READ COMMITTED: every
      statement sees the latest committed data, exactly like a fresh UoW per
      call). It is NEVER the caller's transaction, so — as over RPC — the
      provider does not see the caller's uncommitted writes. It is always
      rolled back: when the scope ends, when a call raises or leaves ORM
      writes pending, and once the request stops making read calls —
      ``idle_seconds=0`` (default) releases it on the next event-loop turn
      after a call, i.e. as soon as the request awaits anything else, so
      back-to-back calls share it but it never idles in the pool's absence
      while the request does other I/O; ``>0`` keeps it that long; ``None``
      holds it until the scope ends. A call that finds the shared connection
      busy (concurrent tasks, a read contract calling another on the same
      database) falls back to its own Unit of Work. A call that finds the
      shared transaction aborted by an earlier call's swallowed database
      error (SQLSTATE 25P02) drops that connection and reruns in its own
      Unit of Work (``stats["replaced"]``);
    * identical read calls (local or RPC; same contract, method, bound tenant and
      arguments) are memoised for the rest of the scope — each caller still
      receives its own copy. Any command call (local or remote) clears the
      memo. External commits are visible after invalidation
      or in a new scope; use ``memo=False`` when every read must be fresh.

    A read contract must not write: a raw DML statement it executed would be
    visible to later read contracts of the same scope until rollback.
    """

    __slots__ = (
        "_units",
        "_memo",
        "_idle",
        "_closed",
        "memo_generation",
        "_closing",
        "_stats",
    )

    def __init__(self, *, memo: bool = True, idle_seconds: float | None = 0.0) -> None:
        # One per HTTP request, and most requests make no local read call:
        # the units, the closing set and the counters are created on first use.
        self._units: dict[tuple[int, str], _ScopedUnit] = _NO_UNITS
        self._memo: dict[Any, Any] | None = {} if memo else None
        self._idle = idle_seconds
        self._closed = False
        self.memo_generation = 0
        self._closing: set[Any] | None = None
        self._stats: dict[str, int] | None = None

    @property
    def stats(self) -> dict[str, int]:
        """Counters (diagnostics / performance budget tests)."""
        stats = self._stats
        if stats is None:
            stats = self._stats = {
                "opened": 0,
                "reused": 0,
                "fallback": 0,
                "released_idle": 0,
                "memo_hits": 0,
                "replaced": 0,
            }
        return stats

    # -- memo -----------------------------------------------------------------
    @property
    def memo_enabled(self) -> bool:
        return self._memo is not None

    def memo_get(self, key: Any) -> Any:
        memo = self._memo
        if memo is None:
            return _MEMO_MISS
        value = memo.get(key, _MEMO_MISS)
        if value is not _MEMO_MISS:
            self.stats["memo_hits"] += 1
        return value

    def memo_set(self, key: Any, value: Any) -> None:
        if self._memo is not None and not self._closed:
            self._memo[key] = value

    def invalidate(self) -> None:
        """Forget memoised reads (a command may have changed what they return)."""
        self.memo_generation += 1
        if self._memo:
            self._memo.clear()

    # -- shared read unit -----------------------------------------------------
    async def run(  # noqa: C901
        self,
        func: Callable[..., Awaitable[Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        core: Any,
        database: str,
        qualname: str,
    ) -> Any:
        """Run a read contract on the shared unit, or ``_SCOPE_FALLBACK``."""
        if self._closed:
            return _SCOPE_FALLBACK
        key = (id(core), database)
        scoped = self._units.get(key)
        if scoped is not None and scoped.busy:
            self.stats["fallback"] += 1
            return _SCOPE_FALLBACK
        reused = scoped is not None
        if scoped is None:
            if self._units is _NO_UNITS:
                self._units = {}
            scoped = self._units[key] = _ScopedUnit()
            unit: Any = None
            try:
                uow = _unit_of_work
                factory = _contract_uow_factory(core, database)
                unit = factory()
                await unit.__aenter__()
                session = uow._bind_uow_session(unit.session, core, factory, database)
            except BaseException:
                if self._units.get(key) is scoped:
                    del self._units[key]
                scoped.done()
                if unit is not None and getattr(unit, "session", None) is not None:
                    self._close_later(unit)
                raise
            scoped.unit, scoped.session = unit, session
            self.stats["opened"] += 1
        else:
            scoped.busy = True
            if scoped.timer is not None:
                scoped.timer.cancel()
                scoped.timer = None
            self.stats["reused"] += 1
        session = scoped.session
        # Exclusive while busy: the calling task owns the shared unit now.
        _request.claim_owner(session)
        keep = False
        uow = _unit_of_work
        try:
            _context.stamp_caller(session)
            token = _contract_session_var.set(session)
            try:
                with (
                    _isolated_uow_scope(session, database),
                    _tenant_context.configured_tenant_scope(
                        getattr(core, "settings", None)
                    ),
                    _guard_read_contract(session, read_contract_write_error(qualname)),
                ):
                    result = await func(*args, **kwargs)
            finally:
                _contract_session_var.reset(token)
            wrote = _read_contract_writes(session)
            if wrote is not None:
                # the shared transaction is dropped below (keep stays False)
                raise read_contract_write_error(qualname)(wrote)
            # Pending ORM changes would leak into the next call's view of the
            # shared transaction: roll it back (as a per-call UoW would).
            keep = not (
                getattr(session, "new", None)
                or getattr(session, "dirty", None)
                or getattr(session, "deleted", None)
            )
            expunge_all = getattr(session, "expunge_all", None)
            if keep and expunge_all is not None:
                # A fresh view per call: no identity-map carry-over.
                expunge_all()
            return result
        except Exception as exc:
            if reused and _in_failed_transaction(exc):
                # An earlier call swallowed a database error and left the
                # shared transaction aborted: drop it (below) and run this call
                # in its own Unit of Work — what it would have done unscoped.
                self.stats["replaced"] += 1
                return _SCOPE_FALLBACK
            raise
        finally:
            scoped.busy = False
            # A unit the scope stopped tracking while busy (``aclose``) is
            # closed here; ``release_connections`` waits for the read instead.
            if not keep or self._closed:
                if self._units.get(key) is scoped:
                    del self._units[key]
                self._close_later(scoped.unit)
            else:
                self._arm_idle(key, scoped)
            if scoped.idle is not None:  # a drain waits for this read
                scoped.idle.set()

    def _arm_idle(self, key: tuple[int, str], scoped: _ScopedUnit) -> None:
        if self._idle is None:
            return  # held until the scope ends
        loop = asyncio.get_running_loop()
        if self._idle <= 0:
            # Next loop turn: runs only once this task suspends on something
            # else; a back-to-back call has re-marked the unit busy by then.
            scoped.timer = loop.call_soon(self._on_idle, key, scoped)
        else:
            scoped.timer = loop.call_later(self._idle, self._on_idle, key, scoped)

    def _on_idle(self, key: tuple[int, str], scoped: _ScopedUnit) -> None:
        scoped.timer = None
        if scoped.busy or self._units.get(key) is not scoped:
            return
        del self._units[key]
        self.stats["released_idle"] += 1
        self._close_later(scoped.unit)

    def _close_later(self, unit: Any) -> None:
        """Roll back + release ``unit`` in the background (never in a finally)."""
        task = asyncio.get_running_loop().create_task(_rollback_unit(unit))
        closing = self._closing
        if closing is None:
            closing = self._closing = set()
        closing.add(task)
        task.add_done_callback(closing.discard)

    async def release_connections(self) -> None:
        """Roll back and release every shared connection NOW; the scope stays
        usable (a later read reopens one). The operation-intent windows
        (``external_call`` / ``release_check``) release it first (spec §4.6).

        A read still running on a shared connection (another task) is waited
        for, then its unit is closed: no read connection survives the drain.
        A read that cannot finish first — the caller runs inside it — is
        refused (SANDLOOM-R008) instead of waited for."""
        if self._units is _NO_UNITS and not self._closing:
            return
        while True:
            busy = [scoped for scoped in self._units.values() if scoped.busy]
            if not busy:
                break
            current = _contract_session_var.get()
            if current is not None and any(s.session is current for s in busy):
                raise _request.UnitRuleError(
                    "an external_call() / release_check() inside a running "
                    "read contract: the read holds a connection it cannot "
                    "release first (SANDLOOM-R008)"
                )
            # The window is already open, so no new read starts meanwhile.
            await asyncio.gather(*(scoped.wait_done() for scoped in busy))
        units, self._units = list(self._units.values()), {}
        for scoped in units:
            if scoped.timer is not None:
                scoped.timer.cancel()
            if scoped.unit is not None:
                self._close_later(scoped.unit)
        if self._closing:
            await asyncio.gather(*tuple(self._closing), return_exceptions=True)

    async def aclose(self) -> None:
        """End the scope: roll back and release every shared connection."""
        self._closed = True
        self.invalidate()
        if self._units is _NO_UNITS and not self._closing:
            return  # no local read call used the scope
        units, self._units = list(self._units.values()), _NO_UNITS
        for scoped in units:
            if scoped.timer is not None:
                scoped.timer.cancel()
            if scoped.unit is not None and not scoped.busy:
                self._close_later(scoped.unit)
        if self._closing:
            await asyncio.gather(*tuple(self._closing), return_exceptions=True)


#: SQLSTATE ``in_failed_sql_transaction`` ("current transaction is aborted").
_FAILED_TRANSACTION_SQLSTATE = "25P02"


def _in_failed_transaction(exc: BaseException) -> bool:
    """True when ``exc`` (or what it wraps) is "current transaction is aborted".

    Duck-typed (``sqlstate`` / ``pgcode`` on the driver error, reached through
    ``orig`` / ``__cause__`` / ``__context__``): the kernel imports no driver.
    """
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attr in ("sqlstate", "pgcode"):
            if getattr(current, attr, None) == _FAILED_TRANSACTION_SQLSTATE:
                return True
        stack.extend(
            (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
        )
    return False


async def _rollback_unit(unit: Any) -> None:
    try:
        await unit.__aexit__(_ReadOnlyRollback, _ReadOnlyRollback(), None)
    except Exception:  # releasing a read-only connection
        _scope_logger.debug("read scope release failed", exc_info=True)


def current_contract_read_scope() -> ContractReadScope | None:
    """The read scope bound to the current context, if any."""
    return _read_scope_var.get()


def invalidate_contract_read_memo() -> None:
    """Clear the current read scope's memo (a write happened elsewhere)."""
    scope = _read_scope_var.get()
    if scope is not None:
        scope.invalidate()


@asynccontextmanager
async def contract_read_scope(
    *, memo: bool | None = None, idle_ms: int | None = None
) -> AsyncIterator[ContractReadScope]:
    """Open a :class:`ContractReadScope` for the block (reuses an outer one).

    The framework opens one per HTTP request; open one explicitly in a
    worker / script that makes several local read-contract calls in a row.
    Memoisation is on unless ``memo=False``; ``idle_ms`` defaults to
    ``CONTRACT_READ_SCOPE_IDLE_MS``, and ``idle_ms < 0`` holds the shared
    connection until the block ends.
    """
    outer = _read_scope_var.get()
    if outer is not None:
        yield outer
        return
    scope = _new_read_scope(memo, idle_ms)
    token = _read_scope_var.set(scope)
    try:
        yield scope
    finally:
        _read_scope_var.reset(token)
        await scope.aclose()


class ContractReadScopeMiddleware:
    """Pure-ASGI middleware opening a :class:`ContractReadScope` per request.

    Lazy: nothing is checked out unless the request makes a local read
    contract call. Disabled by ``CONTRACT_READ_SCOPE_ENABLED=false``.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # ``contract_read_scope()`` unrolled (no async generator per request).
        if (
            scope.get("type") != "http"
            or not settings.contract_read_scope_enabled
            or _read_scope_var.get() is not None  # reuse the outer scope
        ):
            await self.app(scope, receive, send)
            return
        read_scope = _new_read_scope(None, None)
        token = _read_scope_var.set(read_scope)
        try:
            await self.app(scope, receive, send)
        finally:
            _read_scope_var.reset(token)
            await read_scope.aclose()


def _new_read_scope(memo: bool | None, idle_ms: int | None) -> ContractReadScope:
    idle = settings.contract_read_scope_idle_ms if idle_ms is None else idle_ms
    return ContractReadScope(
        memo=True if memo is None else memo,
        idle_seconds=None if idle < 0 else idle / 1000,
    )


def _memo_key(
    contract: str,
    method: str,
    positional: tuple[Any, ...],
    keywords: dict[str, Any],
) -> Any:
    """Memo key of a read call (``None``: arguments not keyable)."""
    tenant = _tenant_context.current_tenant_id_optional()
    try:
        key: Any = (
            contract,
            method,
            tenant,
            tuple((type(v), v) for v in positional),
            tuple((k, type(v), v) for k, v in sorted(keywords.items())),
        )
        hash(key)
        return key
    except TypeError:  # unhashable argument (list, model, ...): digest it
        try:
            return (
                contract,
                method,
                tenant,
                contract_wire.digest({"a": list(positional), "k": keywords}),
            )
        except contract_wire.ContractWireError:
            return None
