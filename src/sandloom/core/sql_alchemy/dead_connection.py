"""Pooled connections that died while idle: detect, classify, recover.

The driver pings a pooled connection at checkout only after it idled for
``DB_POOL_PING_IDLE_SECONDS`` (no unconditional pre-ping round trip). A backend
killed while its connection sat idle for less than that (``pg_terminate_backend``,
an idle-session timeout, a failover) leaves asyncpg in one of two states:

* the FATAL ``57P01`` and the TCP close were both processed: the connection
  reports ``is_closed()``;
* only the FATAL was read (the next checkout raced the close): asyncpg's
  protocol sits in ``ERROR_CONSUME`` waiting for a ReadyForQuery that never
  comes; ``is_closed()`` is still False and nothing Python can read shows it,
  but the next message is refused with ``InternalClientError: cannot switch to
  state N; another operation (2) is in progress`` BEFORE a byte is written.

SQLAlchemy's asyncpg dialect counts an error as a disconnect only when the
connection already reports closed, so the second state used to put the dead
connection back in the pool, and in both states the request failed with a
500. Two separate mechanisms now:

**Connection recovery** (never re-executes anything that reached the server)

1. Checkout (``install_idle_ping`` in the SQLAlchemy driver): a connection
   asyncpg already reports closed is replaced before it is handed out — an
   attribute read, no round trip.
2. First message refused before sending: when the first message of a fresh
   database transaction — asyncpg's ``BEGIN``, the merged ``BEGIN;
   set_config`` (:mod:`sandloom.core.sql_alchemy.tenant_rls`), the plain RLS
   ``set_config``, an autocommit statement, the planner's first lease message
   — is refused by asyncpg WITHOUT being written (:func:`never_sent`: the
   ``ERROR_CONSUME`` state error, "connection is closed", the "failed"
   protocol state, "not connected") and no database transaction is open, the
   pooled connection under the SQLAlchemy ``Connection`` is replaced in place
   and the message sent once on the new one. Provably nothing of it reached
   the server, so this is recovery, not an operation retry. Kill switch:
   ``DB_DEAD_CONNECTION_RETRY=false``.

**Classification** (``handle_error``, a dialect event — no per-statement cost)

Everything else that fails on a dead connection — a statement or COMMIT that
was sent and lost its connection before the reply, whose outcome is therefore
unknown — is NEVER retried: :func:`is_dead_connection_error` (the state error
above, ``ConnectionDoesNotExistError``, "connection is closed", SQLSTATE class
08 / 57P01-57P03, a socket ``ConnectionError``) marks it a disconnect (SQLAlchemy
invalidates this one connection, not its pool peers) and replaces it with
:class:`sandloom.core.exceptions.DatabaseUnavailable` (HTTP 503, ``Retry-After``),
chained to the original error. Other ``InternalClientError`` s — e.g. two
operations on one connection at once, a programming error — are NOT a
disconnect and keep their type.

The in-place replacement drives private SQLAlchemy / asyncpg state (the pool
record, the adapter's ``_connection`` / ``_transaction``, asyncpg's
``_top_xact``); the ``dead_connection_retry`` probe
(:mod:`sandloom.core.sql_alchemy.driver_compat`) verifies it once per process — on
a mismatch only the classification stays.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.ext.asyncio import AsyncEngine

import sandloom.core.exceptions as _exceptions
import sandloom.core.observability.metrics as _metrics
import sandloom.core.sql_alchemy.driver_compat as _driver_compat
from sandloom.core.config.settings import settings

logger = logging.getLogger("sandloom.db.dead_connection")

#: Engine attribute marking the handling as installed (idempotency).
_INSTALLED_ATTR = "_sandloom_dead_connection_installed"
#: asyncpg errors raised BEFORE anything is written to the socket.
_NOT_SENT_RE = re.compile(
    r"cannot switch to state \d+; (another operation \(2\) is in progress"
    r"|protocol is in the \"failed\" state)"
    r"|connection is closed"
    r"|^not connected$"
)
#: Server-side "this session is gone": admin / crash shutdown, cannot connect.
_DEAD_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})
#: Adapter attributes that carry per-checkout transaction characteristics
#: (isolation level, ``postgresql_readonly``, deferrable).
_CHARACTERISTICS = ("isolation_level", "_isolation_setting", "readonly", "deferrable")


def _chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    todo: list[BaseException | None] = [error]
    while todo and len(seen) < 8:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        todo.append(current.__context__)
        todo.append(current.__cause__)


def _driver_errors() -> tuple[type[BaseException], ...]:
    try:
        from asyncpg import exceptions  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
    except Exception:  # noqa: BLE001 - asyncpg not installed
        return ()
    return (
        exceptions.ConnectionDoesNotExistError,
        exceptions.InterfaceError,
        exceptions.InternalClientError,
    )


def never_sent(error: BaseException) -> bool:
    """True if asyncpg refused the message BEFORE writing a byte of it
    because the connection is dead (``ERROR_CONSUME`` / closed / failed)."""
    errors = _driver_errors()
    if not errors:
        return False
    return any(
        isinstance(item, errors[1:]) and _NOT_SENT_RE.search(str(item)) is not None
        for item in _chain(error)
    )


def is_dead_connection_error(error: BaseException) -> bool:
    """True if ``error`` (or its cause chain) says the connection is dead.

    Deliberately narrow: an ``InternalClientError`` counts only in asyncpg's
    dead-connection states, never e.g. for a concurrent-use bug.
    """
    errors = _driver_errors()
    for item in _chain(error):
        if errors and isinstance(item, errors[0]):  # ConnectionDoesNotExist
            return True
        if isinstance(item, ConnectionError):  # reset / broken pipe / refused
            return True
        sqlstate = getattr(item, "sqlstate", None)
        if isinstance(sqlstate, str) and (
            sqlstate in _DEAD_SQLSTATES or sqlstate.startswith("08")
        ):
            return True
    return never_sent(error)


def recoverable(adapter: Any, error: BaseException) -> bool:
    """May the failed message be re-sent on a new connection? (asyncpg)

    Only when asyncpg refused it before writing a byte (:func:`never_sent`)
    AND no database transaction is open on the connection — no top
    transaction (autocommit), or the top transaction is the one whose
    ``BEGIN`` was just refused. A transaction that already ran a statement is
    never recovered: it died with its connection.
    """
    if adapter is None or not never_sent(error):
        return False
    try:
        from asyncpg.transaction import (  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
            TransactionState,
        )
    except Exception:  # noqa: BLE001 - asyncpg not installed
        return False
    if not isinstance(adapter, AsyncAdapt_asyncpg_connection):
        return False  # only asyncpg's transaction bookkeeping is understood
    con = getattr(adapter, "_connection", None)
    if con is None:
        return False
    top = getattr(con, "_top_xact", None)
    if top is None:
        return True
    return (
        getattr(adapter, "_transaction", "?") is None
        and top._state is TransactionState.FAILED
        and not top._nested
    )


def recovery_enabled() -> bool:
    """``DB_DEAD_CONNECTION_RETRY`` on and the driver internals verified."""

    return (
        bool(settings.db_dead_connection_retry)
        and _driver_compat.dead_connection_retry_ok()
    )


def count_recovery(phase: str) -> None:
    _metrics.db_dead_connection_replaced_total.labels(phase=phase).inc()
    logger.info(
        "pooled connection was dead before the transaction's first message was "
        "sent; replaced it and sent the message on the new connection",
        extra={"event": "db.dead_connection_replaced", "phase": phase},
    )


def replace_connection(context: Any, error: BaseException, phase: str) -> bool:
    """Swap the dead pooled connection under ``context`` for a fresh one.

    Only for a message that never reached the server (the caller checks
    :func:`recoverable` / :func:`never_sent`). The pool record reconnects in
    place (the checkout stays the same: the SQLAlchemy ``Connection``, its
    transaction and the pool accounting are untouched), the pool's
    ``checkout`` listeners run for the new connection, the old adapter's
    transaction characteristics are carried over and ``context.cursor`` is
    re-created on it. Returns False (the caller re-raises ``error``) when the
    connection cannot be replaced.
    """
    if context is None or not recovery_enabled():
        return False
    try:
        fairy = context.root_connection._dbapi_connection
        record = fairy._connection_record
        old = fairy.dbapi_connection
    except AttributeError:
        return False
    if record is None or old is None:
        return False  # a detached connection has no pool record to reconnect
    characteristics = {
        name: getattr(old, name) for name in _CHARACTERISTICS if hasattr(old, name)
    }
    # Closing the record clears its finalizers — among them the reset of the
    # characteristics this checkout set (``postgresql_readonly``, isolation)
    # that runs at checkin. They must survive: they reset the NEW connection,
    # which carries the same characteristics, before the next checkout.
    finalizers = list(record.finalize_callback)
    record.invalidate(error)
    try:
        new = record.get_connection()
    except Exception:  # the server is unreachable: report `error`
        logger.info("dead pooled connection: reconnect failed", exc_info=True)
        return False
    fairy.dbapi_connection = new
    for name, value in characteristics.items():
        setattr(new, name, value)
    record.finalize_callback.extend(finalizers)
    pool = fairy._pool
    if pool.dispatch.checkout:
        pool.dispatch.checkout(new, record, fairy)
    context.cursor = context.create_cursor()
    count_recovery(phase)
    return True


def _adapter(context: Any) -> Any:
    try:
        return context.root_connection._dbapi_connection.dbapi_connection
    except AttributeError:
        return None


def _wrap_execute(original: Callable[..., Any]) -> Callable[..., Any]:
    def do_execute(
        cursor: Any, statement: Any, parameters: Any, context: Any = None
    ) -> Any:
        try:
            return original(cursor, statement, parameters, context)
        except Exception as error:
            if not (
                recoverable(_adapter(context), error)
                and replace_connection(context, error, "begin")
            ):
                raise
        return original(context.cursor, statement, parameters, context)

    return do_execute


def _wrap_execute_no_params(original: Callable[..., Any]) -> Callable[..., Any]:
    def do_execute_no_params(cursor: Any, statement: Any, context: Any = None) -> Any:
        try:
            return original(cursor, statement, context)
        except Exception as error:
            if not (
                recoverable(_adapter(context), error)
                and replace_connection(context, error, "begin")
            ):
                raise
        return original(context.cursor, statement, context)

    return do_execute_no_params


def _classify(ctx: Any) -> BaseException | None:
    """``handle_error``: a dead connection is a disconnect (this connection
    only) and the request gets :class:`DatabaseUnavailable` (503)."""
    error = ctx.original_exception
    dead = error is not None and is_dead_connection_error(error)
    if dead and not ctx.is_disconnect:
        ctx.is_disconnect = True
        ctx.invalidate_pool_on_disconnect = False
    if ctx.is_pre_ping or not (dead or ctx.is_disconnect):
        return None
    if not isinstance(error, Exception):
        return None  # a cancellation stays a cancellation

    root: BaseException = error
    while root.__cause__ is not None:
        root = root.__cause__
    sent = "never sent" if never_sent(error) else "sent"
    logger.warning(
        "database connection died mid-request (%s, %s); answering 503",
        type(root).__name__,
        sent,
        extra={"event": "db.connection_died", "error_type": type(root).__name__},
    )
    return _exceptions.DatabaseUnavailable(error)


def install_dead_connection_handling(engine: AsyncEngine | Any) -> None:
    """Install the classification + never-sent recovery on an asyncpg engine.

    Idempotent. Install it BEFORE the other dialect wrappers (read-only policy,
    ``tenant_rls``) so they run outside it: it only ever sees the statement
    itself (the merged BEGIN and the RLS ``set_config`` recover on their own,
    see :mod:`sandloom.core.sql_alchemy.tenant_rls`).
    """
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if getattr(sync_engine, _INSTALLED_ATTR, False):
        return
    dialect = sync_engine.dialect
    if dialect.driver != "asyncpg":
        return
    event.listen(sync_engine, "handle_error", _classify)
    dialect.do_execute = _wrap_execute(dialect.do_execute)  # type: ignore[method-assign]
    dialect.do_executemany = _wrap_execute(dialect.do_executemany)  # type: ignore[method-assign]
    dialect.do_execute_no_params = _wrap_execute_no_params(  # type: ignore[method-assign]
        dialect.do_execute_no_params
    )
    setattr(sync_engine, _INSTALLED_ATTR, True)


__all__ = [
    "count_recovery",
    "install_dead_connection_handling",
    "is_dead_connection_error",
    "never_sent",
    "recoverable",
    "recovery_enabled",
    "replace_connection",
]
