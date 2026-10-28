"""Capability probes for the driver internals behind the DB fast paths.

Three round-trip optimisations, the dead-connection retry and the pool's
non-blocking checkout reach PRIVATE attributes of asyncpg and of SQLAlchemy
(there is no public API for them):

* merged BEGIN (:mod:`sandloom.core.sql_alchemy.tenant_rls`, ``DB_RLS_MERGED_BEGIN``)
  drives SQLAlchemy's asyncpg adapter (``_transaction``, ``_execute_mutex``,
  ``_connection``, the cursor's ``_adapt_connection``) and asyncpg's
  transaction bookkeeping (``Connection._top_xact`` / ``_protocol``,
  ``Transaction._state``, ``TransactionState.STARTED``);
* the autocommit outbox mark-delivered (:mod:`sandloom.core.sql_alchemy.outbox_store`)
  reads SQLAlchemy's ``SessionTransaction._connections``;
* the dead-connection retry (:mod:`sandloom.core.sql_alchemy.dead_connection`)
  reconnects a pool record in place (``_ConnectionFairy._connection_record`` /
  ``dbapi_connection``, ``_ConnectionRecord.invalidate`` / ``get_connection``)
  and reads asyncpg's ``_top_xact`` / ``Transaction._state`` / ``_nested``
  (probe ``dead_connection_retry``, static checks only; on a mismatch a dead
  connection is still a disconnect, the request is just not retried);
* the non-blocking checkout of
  :class:`sandloom.core.drivers.linger_pool.LingeringQueuePool` takes an idle
  connection straight from SQLAlchemy's pool queue (``QueuePool._pool``,
  ``AsyncAdaptedQueue.get_nowait`` / ``empty``, ``sqlalchemy.util.queue.Empty``)
  instead of the blocking ``asyncio.wait_for(queue.get())`` path (probe
  ``pool_fast_checkout``; on a mismatch every checkout takes the stock path).

A dependency upgrade may rename or change any of them. Each fast path
therefore asks its probe first; the probe runs ONCE per process (cached) and
checks every name it needs (and that it is an attribute / callable of the
expected kind) plus a cheap, I/O-free behavioural self-test:

* ``asyncpg_transaction``: runs asyncpg's REAL ``Transaction.start()`` against
  an in-memory stub connection for every isolation / read-only / deferrable
  combination and requires (a) the BEGIN text to equal the one the merged
  BEGIN sends and (b) the resulting state (``_top_xact``, ``_state``) to equal
  the state the merged BEGIN assigns by hand; then it commits and rolls back a
  hand-adopted transaction through asyncpg's real ``commit()`` /
  ``rollback()`` and requires ``COMMIT;`` / ``ROLLBACK;`` and a cleared
  ``_top_xact``.
* ``session_connections``: an in-memory SQLite ``Session`` must show its
  engine in ``SessionTransaction._connections`` exactly after a connection is
  procured.

A failing probe never fails a request: it logs ONE warning, increments
``sandloom_db_driver_fast_path_disabled_total{probe=...}`` and the fast path
falls back to the plain path (a separate ``BEGIN`` + ``set_config``; a normal
BEGIN for lazy reads; a transactional mark-delivered).

Verified driver versions (``docs/operations/operator-reference.md`` →
"Upgrading asyncpg / SQLAlchemy"): :data:`VERIFIED_DRIVER_RANGES`,
pinned in ``pyproject.toml``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from importlib import metadata
from typing import Any

import sandloom.core.observability.metrics as _metrics

logger = logging.getLogger("sandloom.db.driver_compat")

#: Driver versions the fast paths were verified against: ``[low, high)``.
#: Keep in sync with ``pyproject.toml`` (a test pins both together); raise the
#: upper bound only after the procedure in ``docs/operations/operator-reference.md``.
VERIFIED_DRIVER_RANGES: dict[str, tuple[str, str]] = {
    "asyncpg": ("0.32.0", "0.33"),
    "sqlalchemy": ("2.1.4", "2.2"),
}

ASYNCPG_TRANSACTION = "asyncpg_transaction"
SESSION_CONNECTIONS = "session_connections"
DEAD_CONNECTION_RETRY = "dead_connection_retry"
POOL_FAST_CHECKOUT = "pool_fast_checkout"

_ISOLATION_SQL = {
    "read_committed": " ISOLATION LEVEL READ COMMITTED",
    "read_uncommitted": " ISOLATION LEVEL READ UNCOMMITTED",
    "repeatable_read": " ISOLATION LEVEL REPEATABLE READ",
    "serializable": " ISOLATION LEVEL SERIALIZABLE",
}


def begin_sql(isolation: str | None, readonly: bool, deferrable: bool) -> str:
    """``BEGIN …`` exactly as asyncpg's ``Transaction.start`` builds it (no
    trailing ``;``) — the probe's self-test proves the equality."""
    sql = "BEGIN" + _ISOLATION_SQL.get(isolation or "", "")
    if readonly:
        sql += " READ ONLY"
    if deferrable:
        sql += " DEFERRABLE"
    return sql


def adopt_started_transaction(adapter: Any, con: Any, transaction: Any) -> None:
    """Give asyncpg + SQLAlchemy's adapter the state ``Transaction.start()`` +
    ``_start_transaction()`` would have left after a successful ``BEGIN``."""
    from asyncpg.transaction import TransactionState  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect

    con._top_xact = transaction
    transaction._state = TransactionState.STARTED
    if adapter is not None:
        adapter._transaction = transaction


@dataclass(frozen=True)
class DriverProbe:
    """The verdict of one probe: ``ok`` or the list of what is missing."""

    name: str
    ok: bool
    problems: tuple[str, ...] = ()


# --- static checks ---------------------------------------------------------


def _need_attr(cls: Any, name: str, problems: list[str]) -> None:
    if not hasattr(cls, name):
        problems.append(f"{cls.__module__}.{cls.__qualname__}.{name} is missing")


def _need_callable(cls: Any, name: str, problems: list[str]) -> None:
    member = getattr(cls, name, None)
    if member is None:
        problems.append(f"{cls.__module__}.{cls.__qualname__}.{name}() is missing")
    elif not callable(member):
        problems.append(f"{cls.__module__}.{cls.__qualname__}.{name} is not callable")


def _run_now(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive a coroutine that never suspends (stubbed I/O) to completion."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise RuntimeError("the self-test coroutine suspended (unexpected I/O)")


class _StubProtocol:
    def is_in_transaction(self) -> bool:
        return False


class _StubConnection:
    """Just enough of ``asyncpg.Connection`` for ``Transaction`` to run."""

    def __init__(self) -> None:
        self._top_xact: Any = None
        self._protocol = _StubProtocol()
        self._pool_release_ctr = 0
        self.sent: list[str] = []

    def is_closed(self) -> bool:
        return False

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> str:
        self.sent.append(query)
        return ""


def _asyncpg_selftest(problems: list[str]) -> None:
    from asyncpg.transaction import (  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
        Transaction,
        TransactionState,
    )

    for isolation in (None, *_ISOLATION_SQL):
        for readonly in (False, True):
            for deferrable in (False, True):
                con = _StubConnection()
                real = Transaction(con, isolation, readonly, deferrable)
                _run_now(real.start())
                want = begin_sql(isolation, readonly, deferrable) + ";"
                if con.sent != [want]:
                    problems.append(
                        f"asyncpg BEGIN text changed: {con.sent!r} != {[want]!r}"
                    )
                    return
                if con._top_xact is not real or real._state is not (
                    TransactionState.STARTED
                ):
                    problems.append(
                        "asyncpg Transaction.start() no longer leaves "
                        "(_top_xact=self, _state=STARTED)"
                    )
                    return
    for finish, query, final in (
        ("commit", "COMMIT;", TransactionState.COMMITTED),
        ("rollback", "ROLLBACK;", TransactionState.ROLLEDBACK),
    ):
        con = _StubConnection()
        adopted = Transaction(con, None, False, False)
        adopt_started_transaction(None, con, adopted)
        _run_now(getattr(adopted, finish)())
        if (
            con.sent != [query]
            or con._top_xact is not None
            or adopted._state is not final
        ):
            problems.append(
                f"asyncpg Transaction.{finish}() of an adopted transaction "
                f"behaves differently (sent {con.sent!r})"
            )


def _asyncpg_transaction_problems() -> list[str]:  # noqa: C901
    problems: list[str] = []
    try:
        import asyncpg.connection  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
        import asyncpg.protocol  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
        import asyncpg.transaction  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
        from sqlalchemy.dialects.postgresql import asyncpg as sa_asyncpg  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
    except Exception as error:  # noqa: BLE001 - any import failure = unusable
        return [f"cannot import the asyncpg driver internals: {error!r}"]
    state = getattr(asyncpg.transaction, "TransactionState", None)
    if state is None or not all(
        hasattr(state, m) for m in ("NEW", "STARTED", "COMMITTED", "ROLLEDBACK")
    ):
        problems.append("asyncpg.transaction.TransactionState changed")
    transaction = getattr(asyncpg.transaction, "Transaction", None)
    if transaction is None:
        problems.append("asyncpg.transaction.Transaction is missing")
    else:
        _need_attr(transaction, "_state", problems)
        for meth in ("start", "commit", "rollback"):
            _need_callable(transaction, meth, problems)
    connection = asyncpg.connection.Connection
    for attr in ("_top_xact", "_protocol", "_pool_release_ctr"):
        _need_attr(connection, attr, problems)
    for meth in ("transaction", "execute", "terminate", "is_in_transaction"):
        _need_callable(connection, meth, problems)
    _need_callable(asyncpg.protocol.Protocol, "is_in_transaction", problems)
    adapter = sa_asyncpg.AsyncAdapt_asyncpg_connection
    for attr in (
        "_transaction",
        "_execute_mutex",
        "_connection",
        "isolation_level",
        "readonly",
        "deferrable",
    ):
        _need_attr(adapter, attr, problems)
    for meth in ("_handle_exception", "_start_transaction"):
        _need_callable(adapter, meth, problems)
    _need_attr(sa_asyncpg.AsyncAdapt_asyncpg_cursor, "_adapt_connection", problems)
    if not problems:
        try:
            _asyncpg_selftest(problems)
        except Exception as error:  # noqa: BLE001 - a broken self-test = unusable
            problems.append(f"asyncpg transaction self-test failed: {error!r}")
    return problems


def _session_connections_problems() -> list[str]:
    try:
        from sqlalchemy import create_engine  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
        from sqlalchemy.orm import Session  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback

        engine = create_engine("sqlite://")
    except Exception as error:  # noqa: BLE001 - no in-memory engine: cannot verify
        return [f"cannot build the self-test engine: {error!r}"]
    problems: list[str] = []
    try:
        with Session(engine) as session:
            tx = session.begin()
            connections = getattr(tx, "_connections", None)
            if not isinstance(connections, dict):
                return ["sqlalchemy SessionTransaction._connections is not a dict"]
            if engine in connections:
                problems.append("SessionTransaction._connections pre-populated")
            session.connection()
            if engine not in connections:
                problems.append(
                    "SessionTransaction._connections does not track the "
                    "procured connection by engine"
                )
    except Exception as error:  # noqa: BLE001 - reported as a self-test problem
        problems.append(f"session self-test failed: {error!r}")
    finally:
        engine.dispose()
    return problems


def _dead_connection_retry_problems() -> list[str]:
    """What :mod:`sandloom.core.sql_alchemy.dead_connection` reconnects through."""
    problems: list[str] = []
    try:
        import asyncpg.connection  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
        import asyncpg.transaction  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
        from sqlalchemy.dialects.postgresql import asyncpg as sa_asyncpg  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
        from sqlalchemy.engine.default import DefaultExecutionContext  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
        from sqlalchemy.pool import base as pool_base  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
    except Exception as error:  # noqa: BLE001 - any import failure = unusable
        return [f"cannot import the driver internals: {error!r}"]
    _need_attr(asyncpg.connection.Connection, "_top_xact", problems)
    for attr in ("_state", "_nested"):
        _need_attr(asyncpg.transaction.Transaction, attr, problems)
    state = getattr(asyncpg.transaction, "TransactionState", None)
    if state is None or not hasattr(state, "FAILED"):
        problems.append("asyncpg.transaction.TransactionState.FAILED is missing")
    adapter = sa_asyncpg.AsyncAdapt_asyncpg_connection
    for attr in ("_connection", "_transaction", "isolation_level", "readonly"):
        _need_attr(adapter, attr, problems)
    fairy = pool_base._ConnectionFairy
    for attr in ("_connection_record", "dbapi_connection", "_pool"):
        _need_attr(fairy, attr, problems)
    for meth in ("invalidate", "get_connection"):
        _need_callable(pool_base._ConnectionRecord, meth, problems)
    _need_attr(pool_base._ConnectionRecord, "finalize_callback", problems)
    _need_callable(DefaultExecutionContext, "create_cursor", problems)
    return problems


def _pool_fast_checkout_problems() -> list[str]:  # noqa: C901
    """What ``LingeringQueuePool._do_get``'s non-blocking checkout relies on:
    the queue API it calls, and that the queue hands back the item the stock
    blocking ``get`` would (LIFO / FIFO order, ``Empty`` when drained)."""
    problems: list[str] = []
    try:
        from sqlalchemy.pool import AsyncAdaptedQueuePool, QueuePool  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
        from sqlalchemy.util import queue as sqla_queue  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
    except Exception as error:  # noqa: BLE001 - any import failure = unusable
        return [f"cannot import the SQLAlchemy pool internals: {error!r}"]
    empty = getattr(sqla_queue, "Empty", None)
    if not (isinstance(empty, type) and issubclass(empty, Exception)):
        problems.append("sqlalchemy.util.queue.Empty is missing")
    for meth in ("_do_get", "_do_return_conn", "_inc_overflow", "_dec_overflow"):
        _need_callable(QueuePool, meth, problems)
    queue_class = getattr(AsyncAdaptedQueuePool, "_queue_class", None)
    if queue_class is None:
        problems.append("AsyncAdaptedQueuePool._queue_class is missing")
        return problems
    for meth in ("empty", "get_nowait", "put_nowait", "get", "qsize"):
        _need_callable(queue_class, meth, problems)
    if problems:
        return problems
    try:
        for use_lifo, want in ((True, ["b", "a"]), (False, ["a", "b"])):
            q = queue_class(2, use_lifo=use_lifo)
            q.put_nowait("a")
            q.put_nowait("b")
            got = []
            while not q.empty():
                got.append(q.get_nowait())
            if got != want:
                problems.append(
                    f"pool queue (use_lifo={use_lifo}) order changed: {got!r}"
                )
            try:
                q.get_nowait()
            except empty:  # type: ignore[misc]
                pass
            else:
                problems.append("a drained pool queue no longer raises Empty")
    except Exception as error:  # noqa: BLE001 - a broken self-test = unusable
        problems.append(f"pool queue self-test failed: {error!r}")
    return problems


#: probe name -> the function listing its problems (tests monkeypatch these).
PROBES: dict[str, Callable[[], list[str]]] = {
    ASYNCPG_TRANSACTION: _asyncpg_transaction_problems,
    SESSION_CONNECTIONS: _session_connections_problems,
    DEAD_CONNECTION_RETRY: _dead_connection_retry_problems,
    POOL_FAST_CHECKOUT: _pool_fast_checkout_problems,
}

_FAST_PATHS = {
    ASYNCPG_TRANSACTION: "merged BEGIN (DB_RLS_MERGED_BEGIN) falls back to a "
    "separate BEGIN",
    SESSION_CONNECTIONS: "the outbox mark-delivered falls back to a normal "
    "transaction (BEGIN / UPDATE / COMMIT)",
    DEAD_CONNECTION_RETRY: "a pooled connection that died while idle is still "
    "classified as a disconnect, but the request that finds it fails instead "
    "of being retried once on a new connection",
    POOL_FAST_CHECKOUT: "every pool checkout takes SQLAlchemy's blocking "
    "asyncio.wait_for(queue.get()) path, also when an idle connection is free",
}

_results: dict[str, DriverProbe] = {}
_lock = threading.Lock()


def _installed(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "not installed"


def probe(name: str) -> DriverProbe:
    """The (cached) verdict of probe ``name``; never raises."""
    result = _results.get(name)
    if result is not None:
        return result
    with _lock:
        result = _results.get(name)
        if result is not None:
            return result
        try:
            problems = tuple(PROBES[name]())
        except Exception as error:  # noqa: BLE001 - a probe never fails startup
            problems = (f"probe crashed: {error!r}",)
        result = DriverProbe(name, not problems, problems)
        _results[name] = result
    if not result.ok:
        _metrics.db_driver_fast_path_disabled_total.labels(probe=name).inc()
        logger.warning(
            "DB driver internals changed (asyncpg %s, SQLAlchemy %s): %s; %s. "
            "Requests keep working on the plain path; "
            "see docs/operations/operator-reference.md "
            "'Upgrading asyncpg / SQLAlchemy'.",
            _installed("asyncpg"),
            _installed("sqlalchemy"),
            "; ".join(result.problems),
            _FAST_PATHS[name],
            extra={"event": "db.driver_fast_path_disabled", "probe": name},
        )
    return result


def asyncpg_fast_paths_ok() -> bool:
    """True if merged BEGIN may drive the asyncpg internals."""
    return probe(ASYNCPG_TRANSACTION).ok


def session_connections_ok() -> bool:
    """True if the autocommit mark-delivered may read ``_connections``."""
    return probe(SESSION_CONNECTIONS).ok


def dead_connection_retry_ok() -> bool:
    """True if a dead pooled connection may be replaced in place and retried."""
    return probe(DEAD_CONNECTION_RETRY).ok


def pool_fast_checkout_ok() -> bool:
    """True if the lingering pool may take an idle connection non-blocking."""
    return probe(POOL_FAST_CHECKOUT).ok


def reset_driver_probes() -> None:
    """Forget cached verdicts (tests only)."""
    with _lock:
        _results.clear()


__all__ = [
    "ASYNCPG_TRANSACTION",
    "DEAD_CONNECTION_RETRY",
    "POOL_FAST_CHECKOUT",
    "PROBES",
    "SESSION_CONNECTIONS",
    "VERIFIED_DRIVER_RANGES",
    "DriverProbe",
    "adopt_started_transaction",
    "asyncpg_fast_paths_ok",
    "begin_sql",
    "dead_connection_retry_ok",
    "pool_fast_checkout_ok",
    "probe",
    "reset_driver_probes",
    "session_connections_ok",
]
