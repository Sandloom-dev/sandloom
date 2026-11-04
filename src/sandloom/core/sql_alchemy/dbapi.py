"""The DBAPI adapter: what the SQLAlchemy driver asks of one DBAPI.

Three kinds of knowledge sit around the SQLAlchemy driver
(``design/infra-agnostic/spec.md``, "Where driver code lives"): the
dialect-neutral driver code (this package), the SQL dialect (the renderer,
:mod:`sandloom.core.sql_alchemy.portable`) and the DBAPI. A
:class:`DbapiAdapter` holds the last: how the DBAPI reports a lost
connection, which connection arguments carry the server timeouts, and the
fast paths that drive its private state (the merged row-security ``BEGIN``,
the planner's raw executor, the client read-only policy, the never-sent
recovery of a dead pooled connection).

The adapter is chosen by the URL's DBAPI (``dialect.driver``: ``asyncpg``,
``aiosqlite``, ``psycopg``). The driver registers the adapters it ships
(:func:`register_dbapi_adapter`) before it builds an engine, and a driver
package may add one. A DBAPI without an adapter gets :data:`PLAIN_DBAPI`:
no fast path, nothing classified beyond a socket ``ConnectionError``, the
plain SQLAlchemy path everywhere. That is slower, never wrong.

The hooks ask the adapter once, when they are installed on an engine; no
statement asks the registry.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import exc

from sandloom.core.sql_alchemy.portable import is_error_kind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.sql_alchemy.portable import RowSecurity, SqlDialect

#: The adapter contract this kernel implements; another major is refused.
DBAPI_ADAPTER_CONTRACT = 1

#: The first message of a transaction opens it AND binds the tenant (one
#: round trip instead of two); :meth:`DbapiAdapter.merged_begin`.
MERGED_BEGIN = "merged_begin"
#: The planner's optimized executor may run its statements on the driver
#: connection directly (asyncpg's ``execute`` / ``fetch`` / ``fetchrow`` /
#: ``terminate`` / ``is_in_transaction``, ``$n`` placeholders).
RAW_EXECUTOR = "raw_executor"
#: ``ReadOnlyDbSession`` may refuse writes, locking reads and raw statements
#: in the client, before they are sent.
READ_ONLY_POLICY = "read_only_policy"
#: A dead connection is classified (a disconnect of this connection only,
#: answered with ``DatabaseUnavailable``), a pooled connection the DBAPI
#: reports closed is replaced at checkout, and a first message refused before
#: it was sent is sent again on a new connection.
CONNECTION_RECOVERY = "connection_recovery"
#: Connection attempts after a failure are admitted one at a time, with
#: jittered exponential backoff.
RECONNECT_BACKOFF = "reconnect_backoff"

#: The capabilities only the kernel's own adapters may claim: they drive
#: asyncpg's API (the raw executor), SQLAlchemy's private state (the merged
#: BEGIN) or the kernel's in-place reconnect and probes (connection recovery).
KERNEL_ONLY_CAPABILITIES = frozenset({MERGED_BEGIN, RAW_EXECUTOR, CONNECTION_RECOVERY})

#: Everything an adapter may support.
DBAPI_CAPABILITIES = frozenset(
    {
        MERGED_BEGIN,
        RAW_EXECUTOR,
        READ_ONLY_POLICY,
        CONNECTION_RECOVERY,
        RECONNECT_BACKOFF,
    }
)

#: ``(execution context, SQLAlchemy connection, tenant value) -> merged?``:
#: True when the message opened the transaction and bound the tenant, False
#: when it does not apply (the plain path runs), None when it ran after the
#: pooled connection was found dead and replaced (``context.cursor`` is new).
MergedBegin = Callable[[Any, Any, str], "bool | None"]


def error_chain(error: BaseException) -> Iterator[BaseException]:
    """``error`` and what it wraps (``__context__`` / ``__cause__``), each once,
    at most eight."""
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


class DbapiAdapter:
    """One DBAPI as the SQLAlchemy driver sees it; this base is the plain path.

    A driver package subclasses it only for what makes its DBAPI faster or
    better classified, names the DBAPI in :attr:`name` (SQLAlchemy's
    ``dialect.driver``) and states what it supports in :attr:`capabilities`.
    Every member keeps its plain answer unless overridden. Outside the kernel
    an adapter may claim only ``read_only_policy`` and ``reconnect_backoff``
    (:data:`KERNEL_ONLY_CAPABILITIES`).
    """

    #: The adapter contract this class implements (:data:`DBAPI_ADAPTER_CONTRACT`).
    contract_version: int = DBAPI_ADAPTER_CONTRACT
    #: SQLAlchemy's ``dialect.driver`` for this DBAPI (``asyncpg``).
    name: str = ""
    #: What the adapter supports, from :data:`DBAPI_CAPABILITIES`.
    capabilities: frozenset[str] = frozenset()
    #: With ``raw_executor``: the exact dialect types its driver connection
    #: decodes as SQLAlchemy would, so they are native as planner RESULT
    #: columns (never as keys, filters or order columns).
    raw_result_only_types: tuple[type, ...] = ()

    def server_settings(
        self,
        *,
        statement_timeout_ms: int,
        idle_in_transaction_timeout_ms: int,
        extra: Mapping[str, str] | None,
    ) -> dict[str, str]:
        """The server settings sent when a connection opens (no round trip);
        empty: the DBAPI cannot send them."""
        return {}

    def connect_args(
        self, *, connect_timeout: float, server_settings: Mapping[str, str]
    ) -> dict[str, object]:
        """The DBAPI's ``connect()`` keyword arguments for the connect timeout
        and the server settings; empty: the DBAPI's defaults."""
        return {}

    def verify(self) -> None:
        """Check, once per process, the private DBAPI state the fast paths
        drive. A mismatch warns, counts and leaves the plain path; it never
        raises. Called by the driver when it builds an engine."""

    def connection_lost(self, error: BaseException) -> bool:
        """True when ``error`` says the connection is dead. The plain answer
        asks the renderers (``connection_lost``: on PostgreSQL SQLSTATE class
        08 and 57P01-57P03), so any DBAPI that reports the server's code is
        classified."""
        return is_error_kind(error, "connection_lost")

    def never_sent(self, error: BaseException) -> bool:
        """True when the DBAPI refused the message BEFORE sending a byte of it
        because the connection is dead (``error`` or what it wraps)."""
        return False

    def recoverable(self, dbapi_connection: Any, error: BaseException) -> bool:
        """May the failed message be sent again on a new connection? Only when
        it was never sent and no database transaction is open on
        ``dbapi_connection`` (SQLAlchemy's adapted connection)."""
        return False

    def recovery_supported(self) -> bool:
        """True when a dead pooled connection may be replaced in place."""
        return False

    def refuses_connections(self, error: BaseException) -> bool:
        """True when a failed connection attempt means the server refuses new
        connections for now (too many clients), so the attempt backs off."""
        return False

    def merged_begin(
        self, security: RowSecurity, renderer: SqlDialect
    ) -> MergedBegin | None:
        """The one-message ``BEGIN`` and tenant binding for an engine with
        row security, its ``BEGIN`` texts from ``renderer.begin_statement``;
        ``None``: the plain path (``BEGIN``, then the binding)."""
        return None

    def config_problem(self, url: str) -> str | None:
        """Why the driver cannot open the database ``url`` with this DBAPI
        (an in-memory SQLite, the DBAPI package missing), stated with its
        error code; ``None``: none. Build-free: the URL and the installed
        packages only. Plain: none."""
        return None

    def connection_closed(self, dbapi_connection: Any) -> bool:
        """True when the pooled ``dbapi_connection`` (SQLAlchemy's adapted
        connection) already reports itself closed, read without a round
        trip; the pool then replaces it at checkout. Plain: never known."""
        return False

    def ping(self, dbapi_connection: Any, dialect: Any) -> None:
        """Probe a pooled connection that sat idle, outside a transaction;
        raise when it is dead. Plain: the dialect's own ping."""
        if not dialect.do_ping(dbapi_connection):
            raise exc.DisconnectionError("database ping failed")

    def prepare_engine(self, engine: Any, *, statement_timeout_ms: int) -> None:
        """Install on a newly built ``engine`` (sync or async) what this DBAPI
        needs before its first connection: how it begins transactions, the
        settings each connection opens with. ``statement_timeout_ms`` is the
        runtime pools' statement timeout (0: the default). Plain: nothing."""


#: The adapter of a DBAPI nobody registered one for.
PLAIN_DBAPI = DbapiAdapter()

_adapters: dict[str, DbapiAdapter] = {}
_lock = threading.Lock()


class InvalidDbapiAdapter(TypeError):
    """An adapter that is not a :class:`DbapiAdapter`, has no name, was
    written for another contract major, or claims a capability the kernel
    does not know or keeps to itself (SANDLOOM-B025)."""


def _is_kernel(adapter: DbapiAdapter) -> bool:
    return type(adapter).__module__.startswith("sandloom.core.")


def _check(adapter: DbapiAdapter) -> None:
    if not isinstance(adapter, DbapiAdapter):
        raise InvalidDbapiAdapter(
            f"{adapter!r} is not a sandloom.driverkit.DbapiAdapter [SANDLOOM-B025]"
        )
    if not adapter.name:
        raise InvalidDbapiAdapter(f"{adapter!r} names no DBAPI [SANDLOOM-B025]")
    version = getattr(adapter, "contract_version", None)
    if not isinstance(version, int) or version != DBAPI_ADAPTER_CONTRACT:
        raise InvalidDbapiAdapter(
            f"the DBAPI adapter {adapter.name!r} implements contract "
            f"{version!r}; this kernel implements {DBAPI_ADAPTER_CONTRACT} "
            "[SANDLOOM-B025]"
        )
    unknown = set(adapter.capabilities) - DBAPI_CAPABILITIES
    if unknown:
        raise InvalidDbapiAdapter(
            f"the DBAPI adapter {adapter.name!r} claims unknown capabilities "
            f"{sorted(unknown)}; known: {sorted(DBAPI_CAPABILITIES)} "
            "[SANDLOOM-B025]"
        )
    reserved = set(adapter.capabilities) & KERNEL_ONLY_CAPABILITIES
    if reserved and not _is_kernel(adapter):
        raise InvalidDbapiAdapter(
            f"the DBAPI adapter {adapter.name!r} claims {sorted(reserved)}, "
            "which only the kernel's adapters may: they drive asyncpg's API "
            "and SQLAlchemy's private state [SANDLOOM-B025]"
        )


def register_dbapi_adapter(adapter: DbapiAdapter) -> None:
    """Register ``adapter`` for the DBAPI it names. A later registration
    replaces an earlier one, and a driver package's adapter is never replaced
    by the kernel's shipped one (:func:`register_shipped_dbapi_adapter`)."""
    _check(adapter)
    with _lock:
        _adapters[adapter.name] = adapter


def register_shipped_dbapi_adapter(adapter: DbapiAdapter) -> bool:
    """Register a kernel-shipped ``adapter`` only when no adapter for its DBAPI
    is registered yet; False: a driver package's adapter stays."""
    _check(adapter)
    with _lock:
        if adapter.name in _adapters:
            return False
        _adapters[adapter.name] = adapter
        return True


def dbapi_adapter(driver: str) -> DbapiAdapter:
    """The adapter registered for the DBAPI ``driver``; :data:`PLAIN_DBAPI`
    without one."""
    return _adapters.get(driver, PLAIN_DBAPI)


def dbapi_adapter_of(bind: Any) -> DbapiAdapter:
    """The adapter of ``bind``'s DBAPI: an engine (sync or async) or a
    dialect. Ask once per engine, not per statement."""
    dialect = getattr(bind, "dialect", bind)
    return dbapi_adapter(str(getattr(dialect, "driver", "")))


def dbapi_config_problem(url: str) -> str | None:
    """:meth:`DbapiAdapter.config_problem` of the adapter ``url``'s DBAPI
    (startup asks it for every configured database, build-free)."""
    return dbapi_adapter(dbapi_of_url(url)).config_problem(url)


def dbapi_of_url(url: str) -> str:
    """The DBAPI a database URL names (``postgresql+asyncpg://`` ->
    ``asyncpg``); empty when the URL names none (the dialect's default)."""
    return url.split("://", 1)[0].partition("+")[2]


__all__ = [
    "CONNECTION_RECOVERY",
    "DBAPI_ADAPTER_CONTRACT",
    "DBAPI_CAPABILITIES",
    "KERNEL_ONLY_CAPABILITIES",
    "MERGED_BEGIN",
    "PLAIN_DBAPI",
    "RAW_EXECUTOR",
    "READ_ONLY_POLICY",
    "RECONNECT_BACKOFF",
    "DbapiAdapter",
    "InvalidDbapiAdapter",
    "MergedBegin",
    "dbapi_adapter",
    "dbapi_adapter_of",
    "dbapi_config_problem",
    "dbapi_of_url",
    "error_chain",
    "register_dbapi_adapter",
    "register_shipped_dbapi_adapter",
]
