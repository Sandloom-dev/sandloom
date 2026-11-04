"""Pooled connections that died while idle: detect, classify, recover.

The DBAPI says what a dead connection looks like (its
:class:`~sandloom.core.sql_alchemy.dbapi.DbapiAdapter`, chosen by the
engine's DBAPI; asyncpg's: :mod:`sandloom.core.drivers.dbapi_asyncpg`); this
module holds the mechanics, the same for every DBAPI. Installed only where
the adapter supports ``connection_recovery``.

The driver pings a pooled connection at checkout only after it idled for
``DB_POOL_PING_IDLE_SECONDS`` (no unconditional pre-ping round trip). A
backend killed while its connection sat idle for less than that leaves the
connection either reported closed (replaced at checkout, an attribute read)
or still open with its next message refused before it is written (asyncpg's
``ERROR_CONSUME`` state). SQLAlchemy counts an error as a disconnect only when
the connection already reports closed. Two separate mechanisms:

**Connection recovery** (never re-executes anything that reached the server)

When the first message of a fresh database transaction (the DBAPI's
``BEGIN``, the merged ``BEGIN; set_config``
(:mod:`sandloom.core.sql_alchemy.tenant_rls`), the plain RLS binding, an
autocommit statement, the planner's first lease message) is refused WITHOUT
being written (:meth:`DbapiAdapter.never_sent`) and no database transaction
is open, the pooled connection under the SQLAlchemy ``Connection`` is
replaced in place and the message sent once on the new one. Provably nothing
of it reached the server, so this is recovery, not an operation retry. Kill
switch: ``DB_DEAD_CONNECTION_RETRY=false``.

**Classification** (``handle_error``, a dialect event — no per-statement cost)

Everything else that fails on a dead connection — a statement or COMMIT that
was sent and lost its connection before the reply, whose outcome is therefore
unknown — is NEVER retried: :func:`is_dead_connection_error` (the adapter's
:meth:`DbapiAdapter.connection_lost` and ``never_sent``, a socket
``ConnectionError``) marks it a disconnect (SQLAlchemy invalidates this one
connection, not its pool peers) and replaces it with
:class:`sandloom.core.exceptions.DatabaseUnavailable` (HTTP 503,
``Retry-After``), chained to the original error.

The in-place replacement drives private SQLAlchemy state (the pool record)
and the DBAPI's; the adapter verifies both once per process
(:meth:`DbapiAdapter.recovery_supported`) — on a mismatch only the
classification stays.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

import sandloom.core.exceptions as _exceptions
import sandloom.core.observability.metrics as _metrics
from sandloom.core.config.settings import settings
from sandloom.core.sql_alchemy.dbapi import (
    CONNECTION_RECOVERY,
    DbapiAdapter,
    dbapi_adapter_of,
    error_chain,
)

logger = logging.getLogger("sandloom.db.dead_connection")

#: Engine attribute marking the handling as installed (idempotency).
_INSTALLED_ATTR = "_sandloom_dead_connection_installed"
#: Adapter attributes that carry per-checkout transaction characteristics
#: (isolation level, the read-only and deferrable options).
_CHARACTERISTICS = ("isolation_level", "_isolation_setting", "readonly", "deferrable")


def is_dead_connection_error(error: BaseException, dbapi: DbapiAdapter) -> bool:
    """True if ``error`` (or its cause chain) says the connection is dead.

    Deliberately narrow: what ``dbapi`` reports as a lost connection, a socket
    ``ConnectionError``, or a message ``dbapi`` refused unsent on a dead
    connection; never e.g. a concurrent-use bug.
    """
    for item in error_chain(error):
        if dbapi.connection_lost(item) or isinstance(item, ConnectionError):
            return True
    return dbapi.never_sent(error)


def recovery_enabled(dbapi: DbapiAdapter) -> bool:
    """``DB_DEAD_CONNECTION_RETRY`` on and the driver internals verified."""

    return bool(settings.db_dead_connection_retry) and dbapi.recovery_supported()


def count_recovery(phase: str) -> None:
    """Count (and log) one dead pooled connection replaced in ``phase``."""
    _metrics.db_dead_connection_replaced_total.labels(phase=phase).inc()
    logger.info(
        "pooled connection was dead before the transaction's first message was "
        "sent; replaced it and sent the message on the new connection",
        extra={"event": "db.dead_connection_replaced", "phase": phase},
    )


def replace_connection(
    context: Any, error: BaseException, phase: str, dbapi: DbapiAdapter
) -> bool:
    """Swap the dead pooled connection under ``context`` for a fresh one.

    Only for a message that never reached the server (the caller checks
    ``dbapi.recoverable`` / ``dbapi.never_sent``). The pool record reconnects in
    place (the checkout stays the same: the SQLAlchemy ``Connection``, its
    transaction and the pool accounting are untouched), the pool's
    ``checkout`` listeners run for the new connection, the old adapter's
    transaction characteristics are carried over and ``context.cursor`` is
    re-created on it. Returns False (the caller re-raises ``error``) when the
    connection cannot be replaced.
    """
    if context is None or not recovery_enabled(dbapi):
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


def _wrap_execute(
    original: Callable[..., Any], dbapi: DbapiAdapter
) -> Callable[..., Any]:
    def do_execute(
        cursor: Any, statement: Any, parameters: Any, context: Any = None
    ) -> Any:
        try:
            return original(cursor, statement, parameters, context)
        except Exception as error:
            if not (
                dbapi.recoverable(_adapter(context), error)
                and replace_connection(context, error, "begin", dbapi)
            ):
                raise
        return original(context.cursor, statement, parameters, context)

    return do_execute


def _wrap_execute_no_params(
    original: Callable[..., Any], dbapi: DbapiAdapter
) -> Callable[..., Any]:
    def do_execute_no_params(cursor: Any, statement: Any, context: Any = None) -> Any:
        try:
            return original(cursor, statement, context)
        except Exception as error:
            if not (
                dbapi.recoverable(_adapter(context), error)
                and replace_connection(context, error, "begin", dbapi)
            ):
                raise
        return original(context.cursor, statement, context)

    return do_execute_no_params


def _classify(ctx: Any, dbapi: DbapiAdapter) -> BaseException | None:
    """``handle_error``: a dead connection is a disconnect (this connection
    only) and the request gets :class:`DatabaseUnavailable` (503)."""
    error = ctx.original_exception
    dead = error is not None and is_dead_connection_error(error, dbapi)
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
    sent = "never sent" if dbapi.never_sent(error) else "sent"
    logger.warning(
        "database connection died mid-request (%s, %s); answering 503",
        type(root).__name__,
        sent,
        extra={"event": "db.connection_died", "error_type": type(root).__name__},
    )
    return _exceptions.DatabaseUnavailable(error)


def install_dead_connection_handling(engine: AsyncEngine | Any) -> None:
    """Install the classification + never-sent recovery on ``engine`` when its
    DBAPI adapter supports ``connection_recovery`` (asyncpg).

    Idempotent. Install it BEFORE the other dialect wrappers (read-only policy,
    ``tenant_rls``) so they run outside it: it only ever sees the statement
    itself (the merged BEGIN and the RLS ``set_config`` recover on their own,
    see :mod:`sandloom.core.sql_alchemy.tenant_rls`).
    """
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if getattr(sync_engine, _INSTALLED_ATTR, False):
        return
    dialect = sync_engine.dialect
    dbapi = dbapi_adapter_of(dialect)
    if CONNECTION_RECOVERY not in dbapi.capabilities:
        return

    def classify(ctx: Any) -> BaseException | None:
        return _classify(ctx, dbapi)

    event.listen(sync_engine, "handle_error", classify)
    dialect.do_execute = _wrap_execute(dialect.do_execute, dbapi)  # type: ignore[method-assign]
    dialect.do_executemany = _wrap_execute(dialect.do_executemany, dbapi)  # type: ignore[method-assign]
    dialect.do_execute_no_params = _wrap_execute_no_params(  # type: ignore[method-assign]
        dialect.do_execute_no_params, dbapi
    )
    setattr(sync_engine, _INSTALLED_ATTR, True)


__all__ = [
    "count_recovery",
    "install_dead_connection_handling",
    "is_dead_connection_error",
    "recovery_enabled",
    "replace_connection",
]
