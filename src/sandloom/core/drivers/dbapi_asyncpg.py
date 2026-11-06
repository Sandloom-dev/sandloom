"""asyncpg's DBAPI adapter: its errors, its connect arguments, its fast paths.

Everything here drives asyncpg or SQLAlchemy's asyncpg adapter; the
mechanics around it are dialect- and DBAPI-neutral
(:mod:`sandloom.core.sql_alchemy.dead_connection`,
:mod:`sandloom.core.sql_alchemy.tenant_rls`,
:mod:`sandloom.core.sql_alchemy.read_only_policy`, the planner adapter). The
SQLAlchemy driver registers :data:`ASYNCPG` before it builds an engine.

**Dead connections.** A backend killed while its connection sat idle
(``pg_terminate_backend``, an idle-session timeout, a failover) leaves asyncpg
in one of two states:

* the FATAL ``57P01`` and the TCP close were both processed: the connection
  reports ``is_closed()``;
* only the FATAL was read (the next checkout raced the close): asyncpg's
  protocol sits in ``ERROR_CONSUME`` waiting for a ReadyForQuery that never
  comes; ``is_closed()`` is still False and nothing Python can read shows it,
  but the next message is refused with ``InternalClientError: cannot switch to
  state N; another operation (2) is in progress`` BEFORE a byte is written
  (:meth:`AsyncpgAdapter.never_sent`).

A lost connection is ``ConnectionDoesNotExistError``, SQLSTATE class 08 or
57P01-57P03 (the renderer's ``connection_lost`` codes), or a message refused
unsent. Other ``InternalClientError`` s — e.g. two operations on one
connection at once, a programming error — are not.

**Merged BEGIN** (``DB_RLS_MERGED_BEGIN``): when the first statement of a
transaction finds SQLAlchemy's adapter with no transaction yet, ONE
simple-query message opens the transaction and binds the tenant
(PostgreSQL's simple protocol runs the statements in order and answers with
one ReadyForQuery)::

    BEGIN [ISOLATION LEVEL …] [READ ONLY] [DEFERRABLE];
    SELECT set_config('sandloom.tenant_id', '<canonical uuid or empty>', true)

The ``BEGIN`` text of each unit the kernel opens is the renderer's
``begin_statement`` (the planner sends the same); another adapter state
(serializable, deferrable, a read-only unit at the server's default
isolation) is sent as asyncpg's own ``Transaction.start`` would. After the
reply the adapter is given what ``_start_transaction`` would have: a started
asyncpg ``Transaction`` registered as the connection's top transaction, so
SQLAlchemy's COMMIT / ROLLBACK / savepoints proceed unchanged. Any failure or
cancellation while that message is in flight TERMINATES the driver
connection, and the error reaches SQLAlchemy as a disconnect.

**Probes.** The merged BEGIN and the dead-connection retry reach PRIVATE
attributes of asyncpg and SQLAlchemy; each is verified once per process
(:mod:`sandloom.core.sql_alchemy.driver_compat`):

* ``asyncpg_transaction``: runs asyncpg's REAL ``Transaction.start()`` against
  an in-memory stub connection for every isolation / read-only / deferrable
  combination and requires (a) the BEGIN text to equal :func:`begin_sql` and
  (b) the resulting state (``_top_xact``, ``_state``) to equal the state the
  merged BEGIN assigns by hand; then it commits and rolls back a hand-adopted
  transaction through asyncpg's real ``commit()`` / ``rollback()`` and
  requires ``COMMIT;`` / ``ROLLBACK;`` and a cleared ``_top_xact``.
* ``dead_connection_retry``: the pool record reconnect
  (``_ConnectionFairy._connection_record`` / ``dbapi_connection``,
  ``_ConnectionRecord.invalidate`` / ``get_connection``) and asyncpg's
  ``_top_xact`` / ``Transaction._state`` / ``_nested`` (static checks only; on
  a mismatch a dead connection is still a disconnect, the request is just not
  retried).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from functools import cache
from typing import Any

from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.util.concurrency import await_

from sandloom.core.drivers.sql_dialects import pg_connection_lost
from sandloom.core.sql_alchemy.dbapi import (
    CONNECTION_RECOVERY,
    MERGED_BEGIN,
    RAW_EXECUTOR,
    READ_ONLY_POLICY,
    RECONNECT_BACKOFF,
    DbapiAdapter,
    MergedBegin,
    error_chain,
)
from sandloom.core.sql_alchemy.dead_connection import replace_connection
from sandloom.core.sql_alchemy.driver_compat import (
    need_attr,
    need_callable,
    probe,
    register_probe,
    run_now,
)
from sandloom.core.sql_alchemy.portable import RowSecurity, SqlDialect

ASYNCPG_TRANSACTION = "asyncpg_transaction"
DEAD_CONNECTION_RETRY = "dead_connection_retry"

#: asyncpg errors raised BEFORE anything is written to the socket.
_NOT_SENT_RE = re.compile(
    r"cannot switch to state \d+; (another operation \(2\) is in progress"
    r"|protocol is in the \"failed\" state)"
    r"|connection is closed"
    r"|^not connected$"
)
#: SQLSTATE ``too_many_connections``: the server refuses new connections now.
_TOO_MANY_CONNECTIONS = "53300"

_ISOLATION_SQL = {
    "read_committed": " ISOLATION LEVEL READ COMMITTED",
    "read_uncommitted": " ISOLATION LEVEL READ UNCOMMITTED",
    "repeatable_read": " ISOLATION LEVEL REPEATABLE READ",
    "serializable": " ISOLATION LEVEL SERIALIZABLE",
}

#: SQLAlchemy adapter state ``(isolation, readonly, deferrable)`` -> the unit
#: ``(write, read_only, snapshot)`` the renderer's ``begin_statement`` writes.
#: The units the kernel opens: a write at the server's default isolation,
#: read-only at read committed or repeatable read, one snapshot.
_RENDERED_UNITS: dict[tuple[str | None, bool, bool], tuple[bool, bool, bool]] = {
    (None, False, False): (True, False, False),
    ("read_committed", True, False): (False, True, False),
    ("repeatable_read", True, False): (False, True, True),
    ("repeatable_read", False, False): (True, False, True),
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


def begin_texts(renderer: SqlDialect) -> dict[tuple[str | None, bool, bool], str]:
    """The merged ``BEGIN`` text per adapter state: the renderer's
    ``begin_statement`` for the units it describes (``None``: the plain
    ``BEGIN``), asyncpg's own text for every other state. Built once per
    engine."""
    texts = {
        (isolation, readonly, deferrable): begin_sql(isolation, readonly, deferrable)
        for isolation in (None, *_ISOLATION_SQL)
        for readonly in (False, True)
        for deferrable in (False, True)
    }
    for state, (write, read_only, snapshot) in _RENDERED_UNITS.items():
        texts[state] = (
            renderer.begin_statement(
                write=write, read_only=read_only, snapshot=snapshot
            )
            or "BEGIN"
        )
    return texts


def adopt_started_transaction(adapter: Any, con: Any, transaction: Any) -> None:
    """Give asyncpg + SQLAlchemy's adapter the state ``Transaction.start()`` +
    ``_start_transaction()`` would have left after a successful ``BEGIN``."""
    from asyncpg.transaction import TransactionState  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect

    con._top_xact = transaction
    transaction._state = TransactionState.STARTED
    if adapter is not None:
        adapter._transaction = transaction


# --- dead connections --------------------------------------------------------


@cache
def _driver_errors() -> tuple[type[BaseException], ...]:
    """asyncpg's connection error classes, looked up once (empty without
    asyncpg)."""
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
        for item in error_chain(error)
    )


def connection_lost(error: BaseException) -> bool:
    """True if ``error`` itself says the connection is dead:
    ``ConnectionDoesNotExistError`` or a ``connection_lost`` SQLSTATE."""
    errors = _driver_errors()
    if errors and isinstance(error, errors[0]):  # ConnectionDoesNotExist
        return True
    sqlstate = getattr(error, "sqlstate", None)
    return isinstance(sqlstate, str) and pg_connection_lost(sqlstate)


def recoverable(adapter: Any, error: BaseException) -> bool:
    """May the failed message be re-sent on a new connection?

    Only when asyncpg refused it before writing a byte (:func:`never_sent`)
    AND no database transaction is open on the connection — no top
    transaction (autocommit), or the top transaction is the one whose
    ``BEGIN`` was just refused. A transaction that already ran a statement is
    never recovered: it died with its connection.
    """
    if adapter is None or not never_sent(error):
        return False
    try:
        from asyncpg.transaction import (  # noqa: PLC0415 - cold: asyncpg is imported only with the asyncpg dialect
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


# --- merged BEGIN -------------------------------------------------------------


def _merged_begin_sql(
    adapter: Any,
    value: str,
    security: RowSecurity,
    texts: Mapping[tuple[str | None, bool, bool], str],
) -> str:
    """The adapter's ``BEGIN …`` (:func:`begin_texts`) followed by the tenant
    binding (literal), in the renderer's words."""
    if value:
        value = str(uuid.UUID(value))  # canonical: hex digits and dashes only
    state = (adapter.isolation_level, adapter.readonly, adapter.deferrable)
    begin = texts.get(state) or begin_sql(*state)
    return security.begin_and_bind(begin, value)


async def _merged_begin(
    adapter: Any,
    value: str,
    security: RowSecurity,
    texts: Mapping[tuple[str | None, bool, bool], str],
) -> bool:
    """Open the adapter's transaction AND set the tenant in one message.

    Returns False (nothing sent) when the adapter already has a transaction.
    """
    async with adapter._execute_mutex:
        if adapter._transaction is not None:
            return False
        con = adapter._connection
        if con._top_xact is not None or con._protocol.is_in_transaction():
            # the same refusal as asyncpg's Transaction.start
            raise RuntimeError("tenant_rls: connection already inside a transaction")
        transaction = con.transaction(
            isolation=adapter.isolation_level,
            readonly=adapter.readonly,
            deferrable=adapter.deferrable,
        )
        sql = _merged_begin_sql(adapter, value, security, texts)
        try:
            await con.execute(sql)  # no args → simple-query protocol
        except BaseException:
            # Transaction state on the server is unknown (BEGIN may have run,
            # the SELECT may have failed or been cancelled mid-flight): never
            # pool this connection. Terminating aborts any server transaction;
            # SQLAlchemy then sees a closed connection → disconnect → invalidate.
            con.terminate()
            raise
        adopt_started_transaction(adapter, con, transaction)
        return True


class _MergedBegin:
    """:data:`~sandloom.core.sql_alchemy.dbapi.MergedBegin` for one engine."""

    __slots__ = ("dbapi", "security", "texts")

    def __init__(
        self,
        dbapi: AsyncpgAdapter,
        security: RowSecurity,
        texts: Mapping[tuple[str | None, bool, bool], str],
    ) -> None:
        self.dbapi = dbapi
        self.security = security
        self.texts = texts

    def __call__(self, context: Any, conn: Any, value: str) -> bool | None:
        """True: merged; False: not applicable (plain path); None: merged,
        after replacing a connection that was found dead (``context.cursor``
        is new)."""
        adapter = getattr(conn.connection, "dbapi_connection", None)
        if (
            adapter is None
            or getattr(adapter, "_transaction", "missing") is not None
            or getattr(adapter, "isolation_level", "autocommit") == "autocommit"
            or not hasattr(adapter, "_execute_mutex")
        ):
            return False
        try:
            return await_(_merged_begin(adapter, value, self.security, self.texts))
        except Exception as error:
            # The merged message IS the transaction's BEGIN: when asyncpg
            # refused it before writing a byte (the connection died while
            # idle), the connection is replaced and the message sent on the
            # new one (``sandloom.core.sql_alchemy.dead_connection``). Once
            # sent: never.
            if not (
                never_sent(error)
                and replace_connection(context, error, "merged_begin", self.dbapi)
            ):
                # translated to the DBAPI error SQLAlchemy expects
                adapter._handle_exception(error)
                raise  # pragma: no cover - _handle_exception always raises
        adapter = conn.connection.dbapi_connection
        try:
            await_(_merged_begin(adapter, value, self.security, self.texts))
        except Exception as error:
            adapter._handle_exception(error)
            raise  # pragma: no cover - _handle_exception always raises
        return None


# --- probes -------------------------------------------------------------------


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
                run_now(real.start())
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
        run_now(getattr(adopted, finish)())
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
        need_attr(transaction, "_state", problems)
        for meth in ("start", "commit", "rollback"):
            need_callable(transaction, meth, problems)
    connection = asyncpg.connection.Connection
    for attr in ("_top_xact", "_protocol", "_pool_release_ctr"):
        need_attr(connection, attr, problems)
    for meth in ("transaction", "execute", "terminate", "is_in_transaction"):
        need_callable(connection, meth, problems)
    need_callable(asyncpg.protocol.Protocol, "is_in_transaction", problems)
    adapter = sa_asyncpg.AsyncAdapt_asyncpg_connection
    for attr in (
        "_transaction",
        "_execute_mutex",
        "_connection",
        "isolation_level",
        "readonly",
        "deferrable",
    ):
        need_attr(adapter, attr, problems)
    for meth in ("_handle_exception", "_start_transaction"):
        need_callable(adapter, meth, problems)
    need_attr(sa_asyncpg.AsyncAdapt_asyncpg_cursor, "_adapt_connection", problems)
    if not problems:
        try:
            _asyncpg_selftest(problems)
        except Exception as error:  # noqa: BLE001 - a broken self-test = unusable
            problems.append(f"asyncpg transaction self-test failed: {error!r}")
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
    need_attr(asyncpg.connection.Connection, "_top_xact", problems)
    for attr in ("_state", "_nested"):
        need_attr(asyncpg.transaction.Transaction, attr, problems)
    state = getattr(asyncpg.transaction, "TransactionState", None)
    if state is None or not hasattr(state, "FAILED"):
        problems.append("asyncpg.transaction.TransactionState.FAILED is missing")
    adapter = sa_asyncpg.AsyncAdapt_asyncpg_connection
    for attr in ("_connection", "_transaction", "isolation_level", "readonly"):
        need_attr(adapter, attr, problems)
    fairy = pool_base._ConnectionFairy
    for attr in ("_connection_record", "dbapi_connection", "_pool"):
        need_attr(fairy, attr, problems)
    for meth in ("invalidate", "get_connection"):
        need_callable(pool_base._ConnectionRecord, meth, problems)
    need_attr(pool_base._ConnectionRecord, "finalize_callback", problems)
    need_callable(DefaultExecutionContext, "create_cursor", problems)
    return problems


register_probe(
    ASYNCPG_TRANSACTION,
    _asyncpg_transaction_problems,
    "merged BEGIN (DB_RLS_MERGED_BEGIN) falls back to a separate BEGIN",
)
register_probe(
    DEAD_CONNECTION_RETRY,
    _dead_connection_retry_problems,
    "a pooled connection that died while idle is still classified as a "
    "disconnect, but the request that finds it fails instead of being retried "
    "once on a new connection",
)


def asyncpg_fast_paths_ok() -> bool:
    """True if merged BEGIN may drive the asyncpg internals."""
    return probe(ASYNCPG_TRANSACTION).ok


def dead_connection_retry_ok() -> bool:
    """True if a dead pooled connection may be replaced in place and retried."""
    return probe(DEAD_CONNECTION_RETRY).ok


# --- the adapter --------------------------------------------------------------


class AsyncpgAdapter(DbapiAdapter):
    """asyncpg: every fast path (see the module doc)."""

    name = "asyncpg"
    capabilities = frozenset(
        {
            MERGED_BEGIN,
            RAW_EXECUTOR,
            READ_ONLY_POLICY,
            CONNECTION_RECOVERY,
            RECONNECT_BACKOFF,
        }
    )
    #: ``citext`` has no SQLAlchemy result processing and asyncpg decodes it to
    #: the same ``str`` (text I/O) on both executors.
    raw_result_only_types = (CITEXT,)

    def server_settings(
        self,
        *,
        statement_timeout_ms: int,
        idle_in_transaction_timeout_ms: int,
        extra: Mapping[str, str] | None,
    ) -> dict[str, str]:
        """PostgreSQL GUCs sent in the startup packet (zero extra round trips)."""
        values: dict[str, str] = {}
        if statement_timeout_ms:
            values["statement_timeout"] = str(statement_timeout_ms)
        if idle_in_transaction_timeout_ms:
            values["idle_in_transaction_session_timeout"] = str(
                idle_in_transaction_timeout_ms
            )
        values.update(extra or {})
        return values

    def connect_args(
        self, *, connect_timeout: float, server_settings: Mapping[str, str]
    ) -> dict[str, object]:
        """asyncpg's ``timeout`` and ``server_settings`` keyword arguments."""
        args: dict[str, object] = {"timeout": connect_timeout}
        if server_settings:
            args["server_settings"] = dict(server_settings)
        return args

    def verify(self) -> None:
        """Run the merged-BEGIN and dead-connection-retry probes (once)."""
        asyncpg_fast_paths_ok()
        dead_connection_retry_ok()

    def connection_lost(self, error: BaseException) -> bool:
        """``ConnectionDoesNotExistError`` or SQLSTATE class 08 / 57P01-57P03."""
        return connection_lost(error)

    def never_sent(self, error: BaseException) -> bool:
        """asyncpg refused the message unsent: ``ERROR_CONSUME``, closed,
        failed or not connected."""
        return never_sent(error)

    def recoverable(self, dbapi_connection: Any, error: BaseException) -> bool:
        """Never sent, and no transaction open in asyncpg's bookkeeping."""
        return recoverable(dbapi_connection, error)

    def recovery_supported(self) -> bool:
        """The ``dead_connection_retry`` probe passed."""
        return dead_connection_retry_ok()

    def refuses_connections(self, error: BaseException) -> bool:
        """SQLSTATE 53300 (``too_many_connections``) on the attempt."""
        return getattr(error, "sqlstate", None) == _TOO_MANY_CONNECTIONS

    def merged_begin(
        self, security: RowSecurity, renderer: SqlDialect
    ) -> MergedBegin | None:
        """The merged BEGIN, or ``None`` when the ``asyncpg_transaction``
        probe failed (it warns and counts once)."""
        if not asyncpg_fast_paths_ok():
            return None
        return _MergedBegin(self, security, begin_texts(renderer))

    def connection_closed(self, dbapi_connection: Any) -> bool:
        """asyncpg's ``is_closed()``: its backend died while it sat in the
        pool and the close was processed (an attribute read, no round trip;
        :mod:`sandloom.core.sql_alchemy.dead_connection` covers the one whose
        close is still in flight)."""
        raw = getattr(dbapi_connection, "_connection", None)
        return raw is not None and bool(raw.is_closed())

    def ping(self, dbapi_connection: Any, dialect: Any) -> None:
        """``SELECT 1`` over the simple-query protocol: ONE round trip, where
        SQLAlchemy's own asyncpg ping wraps a ``;`` in BEGIN/ROLLBACK (three)."""
        raw = getattr(dbapi_connection, "_connection", None)
        if raw is None:
            super().ping(dbapi_connection, dialect)
            return
        # (SQLAlchemy 2.1's adapter has no ``await_`` attribute: the module
        # function, as the tenant_rls hook uses.)
        await_(raw.execute("SELECT 1"))


#: The adapter the SQLAlchemy driver registers for ``postgresql+asyncpg``.
ASYNCPG = AsyncpgAdapter()


__all__ = [
    "ASYNCPG",
    "ASYNCPG_TRANSACTION",
    "DEAD_CONNECTION_RETRY",
    "AsyncpgAdapter",
    "adopt_started_transaction",
    "asyncpg_fast_paths_ok",
    "begin_sql",
    "begin_texts",
    "connection_lost",
    "dead_connection_retry_ok",
    "never_sent",
    "recoverable",
]
