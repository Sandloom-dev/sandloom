"""Dialect hook keeping the PostgreSQL RLS tenant setting in sync (opt-in).

Installed on a database instance's engine when its infra options set
``tenant_rls: true``. Before every statement the connection runs inside a
transaction, the hook makes the transaction-local ``sandloom.tenant_id`` match the
bound tenant (``''`` when none is bound).

There is NO bypass setting any more: the policy created by
:func:`sandloom.core.orm.enable_tenant_rls` has no branch the application role can
switch on. ``unscoped()`` therefore only lifts the ORM guard — on an RLS engine
the database keeps scoping every statement to the bound tenant (zero rows when
none is bound). Cross-tenant work goes through the system channel
(:mod:`sandloom.core.drivers.system_db`): a separate engine logged in as a
separate role.

The setting is applied with ``set_config(..., is_local => true)`` (= ``SET
LOCAL``), so it vanishes at COMMIT/ROLLBACK and never leaks to the next user of
a pooled connection. The hook re-syncs lazily (only when the desired value
differs from what this transaction last set), which covers a tenant bound AFTER
the transaction began (``TenantScope`` resolving after ``DbSession``) and a
``bind_tenant`` switch mid-transaction. The first statement of every
transaction always syncs (one extra round trip per transaction), and a ROLLBACK
TO SAVEPOINT — which reverts ``SET LOCAL`` — forces a re-sync.

Mechanism (why not engine events): the hook wraps the engine dialect's three
statement entry points — ``do_execute``, ``do_executemany`` and
``do_execute_no_params`` — through which every statement SQLAlchemy sends
passes (ORM, Core ``execute``, ``text()``, ``exec_driver_sql``, executemany
and insertmanyvalues batches, server-side/streaming cursors, SAVEPOINT
statements, sequence / default pre-execution, ``session.connection()`` /
``AsyncConnection.run_sync``). Any engine-level ``ConnectionEvents`` listener
(``before_cursor_execute`` but also ``begin`` / ``commit`` / ``rollback``)
switches every ``Connection`` of the engine onto SQLAlchemy's instrumented
execution path (~160k instructions per statement on the ladder). The "value
this transaction last set" lives in the pooled connection's ``info`` (plain
values — no reference cycles); the dialect's ``do_begin`` / ``do_commit`` /
``do_rollback`` (also used by the pool's reset-on-return) are wrapped to drop
it, and a ``ROLLBACK [TO SAVEPOINT]`` statement drops it after it ran.

The outbox's system statements — the post-commit mark-delivered UPDATE and
the relay's claim / settle / sweep — run inside :func:`non_tenant_statements`
(an explicit allowlist: they touch only ``outbox_messages``); with no tenant
bound they skip the setting round trip. Every other transaction keeps the
sync, tenant or not. The scope is SELF-CHECKING: every statement run inside
it must reference only :data:`NON_TENANT_TABLES` (checked on the compiled
statement, ORM eager loads included; cached per compiled form). Raw SQL
(``text()``, ``exec_driver_sql``) is refused unless tagged with the
``sandloom_non_tenant=True`` execution option. A violation raises
:class:`NonTenantStatementError` outside production; in production it is
logged + counted and the tenant setting is synced anyway (fail-safe).

Merged BEGIN (``DB_RLS_MERGED_BEGIN``, default ON; kill switch
``DB_RLS_MERGED_BEGIN=false``; asyncpg only): normally the first statement of
a transaction costs two round trips before it runs — asyncpg's ``BEGIN``
(sent lazily by SQLAlchemy's adapter) and the ``set_config``. With the
option, when that first statement finds the adapter with no transaction yet,
the hook sends ONE simple-query message
(PostgreSQL's simple protocol runs the statements in order and answers with
one ReadyForQuery)::
    BEGIN [ISOLATION LEVEL …] [READ ONLY] [DEFERRABLE];
    SELECT set_config('sandloom.tenant_id', '<canonical uuid or empty>', true)

The tenant is a LITERAL here (the simple protocol has no parameters); it is
only ever ``str(uuid.UUID(...))`` — hex digits and dashes — or ``''``. The
BEGIN text is built exactly as asyncpg's ``Transaction.start`` builds it for
the adapter's isolation / readonly / deferrable. After the reply the hook
gives the adapter what ``_start_transaction`` would have: a started asyncpg
``Transaction`` registered as the connection's top transaction, so
SQLAlchemy's COMMIT / ROLLBACK / savepoints proceed unchanged. Any failure or
cancellation while that message is in flight TERMINATES the driver
connection (the server aborts the transaction, the GUC dies with it) and the
error reaches SQLAlchemy as a disconnect, which invalidates the pooled
connection — a connection whose transaction state is unknown is never pooled.
Nothing is merged when the adapter already has a transaction, in autocommit,
on a non-asyncpg driver, or when the driver-internals probe
(:mod:`sandloom.core.sql_alchemy.driver_compat`) finds the private asyncpg /
SQLAlchemy state it relies on changed — then the plain path (separate BEGIN +
``set_config``) is used, with one warning and a metric.

NOT covered (they bypass SQLAlchemy's statement execution): the raw DB-API /
driver connection (``AsyncConnection.get_raw_connection()``,
``.driver_connection``, ``connection.dbapi_connection.cursor()``) and the
pool's own probes (idle ping, reset-on-return). There the setting is not set,
so the FORCEd policy sees an empty ``sandloom.tenant_id`` and fails closed.

The hook keeps HONEST code consistent; it is not an authentication boundary.
Code that can send arbitrary SQL as the application role can still set
``sandloom.tenant_id`` itself (see ``docs/reference/security-model.md``).

:func:`verify_tenant_rls_roles` checks at startup that the application role
cannot step outside the policies (superuser, ``BYPASSRLS``, table owner, member
of such a role or of the system role).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.expression import RollbackToSavepointClause
from sqlalchemy.sql.selectable import TableClause
from sqlalchemy.util.concurrency import await_

import sandloom.core.observability.metrics as _metrics
from sandloom.core.config.settings import settings
from sandloom.core.sql_alchemy.dead_connection import (
    never_sent,
    recoverable,
    replace_connection,
)
from sandloom.core.sql_alchemy.driver_compat import (
    adopt_started_transaction,
    asyncpg_fast_paths_ok,
    begin_sql,
)
from sandloom.core.sql_alchemy.rls import (
    TENANT_SETTING,
    tenant_rls_policy_name,
)
from sandloom.core.tenant_context import _bypass, _current_tenant

logger = logging.getLogger("sandloom.tenant_rls")

#: conn.info key: the tenant value this transaction last set, or absent.
_STATE_KEY = "sandloom_tenant_rls_state"
#: conn.info key: the unscoped()-without-tenant warning was logged in this txn.
_WARNED_KEY = "sandloom_tenant_rls_unscoped_warned"
#: Engine attribute marking the hook as installed (idempotency).
_INSTALLED_ATTR = "_sandloom_tenant_rls_installed"
#: Engine attribute marking a system-channel engine (no unscoped() warning).
_SYSTEM_ATTR = "_sandloom_tenant_rls_system"
#: A raw / text statement that may roll back (to a savepoint): re-sync after it.
_ROLLBACK_RE = re.compile(r"\brollback\b", re.IGNORECASE)


#: Set by :func:`non_tenant_statements` (framework system code only).
_NON_TENANT: ContextVar[bool] = ContextVar(
    "sandloom_tenant_rls_non_tenant", default=False
)
#: The ONLY tables a :func:`non_tenant_statements` scope may touch: core infra
#: tables that carry no tenant policy.
NON_TENANT_TABLES: frozenset[str] = frozenset({"outbox_messages"})
#: Execution option tagging a raw statement (``text()`` / driver SQL) as
#: touching no tenant table — required for raw SQL inside the scope.
NON_TENANT_OPTION = "sandloom_non_tenant"
#: Compiled-object attribute caching the tables a compiled form references.
_TABLES_ATTR = "_sandloom_rls_tables"


class NonTenantStatementError(RuntimeError):
    """A statement inside :func:`non_tenant_statements` is not allowlisted.

    Raised outside production; in production the statement runs with the
    tenant setting synced (fail-safe) and the violation is logged + counted.
    """


@contextmanager
def non_tenant_statements() -> Iterator[None]:
    """Declare the enclosed statements as touching NO tenant-scoped table.

    An explicit opt-out for a short allowlist of FRAMEWORK system operations
    on core tables without RLS (the outbox mark-delivered UPDATE and the
    relay's claim / settle / sweep).
    Inside it, a transaction with NO bound tenant skips the tenant-setting
    round trip: the statements never read an RLS table, so the setting has
    nothing to protect. Everything else keeps the sync on the first statement
    of every transaction, including every other no-tenant transaction. A
    bound tenant (or a value this transaction already set) is still synced.
    Never wrap application code or a statement on a tenant table in it: every
    statement inside is checked against :data:`NON_TENANT_TABLES` (raw SQL
    needs ``execution_options(sandloom_non_tenant=True)``) — a violation raises
    :class:`NonTenantStatementError` outside production and is synced anyway
    (never skipped) in production.
    """
    token = _NON_TENANT.set(True)
    try:
        yield
    finally:
        _NON_TENANT.reset(token)


class TenantRLSConfigError(RuntimeError):
    """``tenant_rls`` was enabled on a database that cannot enforce it.

    Raised for a non-PostgreSQL backend and by :func:`verify_tenant_rls_roles`
    when a role could step outside the policies. A security misconfiguration,
    so startup aborts on it in EVERY environment (``fatal_at_startup``).
    """

    fatal_at_startup = True


def _info(pooled: Any) -> dict[str, Any]:
    """The per-DBAPI-connection ``info`` dict behind a pooled proxy.

    The pool hands out its fairy (``.info`` = the record's) or, during first
    connect / reset of a finalised connection, an ad-hoc proxy without an
    ``.info`` of its own; both carry ``_connection_record``. A detached fairy
    keeps its own ``.info``.
    """
    record = getattr(pooled, "_connection_record", None)
    return record.info if record is not None else pooled.info  # type: ignore[no-any-return]


def _may_revert(context: Any, statement: Any) -> bool:
    """True if ``statement`` may revert ``SET LOCAL`` (ROLLBACK [TO SAVEPOINT])."""
    compiled = context.compiled
    if compiled is not None:
        stmt = compiled.statement
        if isinstance(stmt, RollbackToSavepointClause):
            return True
        if not isinstance(stmt, TextClause):
            return False  # SELECT / INSERT / UPDATE / DELETE / DDL constructs
    return isinstance(statement, str) and _ROLLBACK_RE.search(statement) is not None


def _warn_unscoped_without_tenant(conn: Any, info: dict[str, Any]) -> None:
    if info.get(_WARNED_KEY) or getattr(conn.engine, _SYSTEM_ATTR, False):
        return
    # unscoped() with no tenant on an RLS engine is almost always code that
    # expects cross-tenant access; the database will show it nothing.
    info[_WARNED_KEY] = True
    logger.warning(
        "unscoped() on a tenant_rls database does not lift Row-Level "
        "Security: with no tenant bound, RLS tables return no rows and "
        "refuse writes. Use the system channel "
        "(sandloom.core.drivers.system_db) for cross-tenant work.",
        extra={"event": "tenant_rls.unscoped_without_tenant"},
    )


def _merged_begin_sql(adapter: Any, value: str) -> str:
    """``BEGIN …;`` exactly as asyncpg's ``Transaction.start`` builds it for the
    adapter's settings, followed by the tenant ``set_config`` (literal)."""
    if value:
        value = str(uuid.UUID(value))  # canonical: hex digits and dashes only
    begin = begin_sql(adapter.isolation_level, adapter.readonly, adapter.deferrable)
    return f"{begin}; SELECT set_config('{TENANT_SETTING}', '{value}', true)"


async def _merged_begin(adapter: Any, value: str) -> bool:
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
        sql = _merged_begin_sql(adapter, value)
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


def _try_merged_begin(context: Any, conn: Any, value: str) -> bool | None:
    """True: merged; False: not applicable (plain path); None: merged, after
    replacing a connection that was found dead (``context.cursor`` is new)."""
    adapter = getattr(conn.connection, "dbapi_connection", None)
    if (
        adapter is None
        or getattr(adapter, "_transaction", "missing") is not None
        or getattr(adapter, "isolation_level", "autocommit") == "autocommit"
        or not hasattr(adapter, "_execute_mutex")
    ):
        return False
    try:
        return await_(_merged_begin(adapter, value))
    except Exception as error:
        # The merged message IS the transaction's BEGIN: when asyncpg refused
        # it before writing a byte (the connection died while idle), the
        # connection is replaced and the message sent on the new one
        # (``sandloom.core.sql_alchemy.dead_connection``). Once sent: never.
        if not (
            never_sent(error) and replace_connection(context, error, "merged_begin")
        ):
            # translated to the DBAPI error SQLAlchemy expects
            adapter._handle_exception(error)
            raise  # pragma: no cover - _handle_exception always raises
    adapter = conn.connection.dbapi_connection
    try:
        await_(_merged_begin(adapter, value))
    except Exception as error:
        adapter._handle_exception(error)
        raise  # pragma: no cover - _handle_exception always raises
    return None


def _sync(context: Any, merged_begin: bool = False) -> bool:  # noqa: C901
    """Bring this transaction's tenant setting in line with the tenant context.

    Returns True when the pooled connection was found dead at the
    transaction's first message and replaced: ``context.cursor`` is new.
    """
    if context is None:  # never on SQLAlchemy's statement paths: refuse
        raise TenantRLSConfigError(
            "tenant_rls: a statement reached the dialect without an execution "
            "context; refusing to run it without the tenant setting"
        )
    conn = context.root_connection
    if not conn._allow_autobegin:
        # SQLAlchemy's own first-connect ``dialect.initialize()`` (server
        # version, schema, string settings): no transaction, no tenant data —
        # the old event hook never saw it either (events off on that Connection).
        return False
    info = _info(conn.connection)
    tenant = _current_tenant.get()
    if tenant is None and _bypass.get():
        _warn_unscoped_without_tenant(conn, info)
    non_tenant = _NON_TENANT.get() and _non_tenant_allowed(context)
    if _STATE_KEY in info:
        if info[_STATE_KEY] == tenant:
            return False
    elif tenant is None and non_tenant:
        return False  # allowlisted system statements on non-tenant tables
    value = str(uuid.UUID(str(tenant))) if tenant is not None else ""
    if merged_begin:
        merged = _try_merged_begin(context, conn, value)
        if merged is not False:
            _info(conn.connection)[_STATE_KEY] = tenant
            return merged is None
    sql, params = _set_config_sql(conn.dialect.paramstyle, value)
    replaced = False
    # A separate DB-API cursor on the SAME connection/transaction: it neither
    # re-enters this hook nor disturbs the cursor about to run the statement.
    raw = conn.connection.cursor()
    try:
        raw.execute(sql, params)
    except Exception as error:
        # asyncpg refused the transaction's lazy BEGIN before writing a byte
        # (the connection died while idle): replace it, send again on the new.
        if not (
            recoverable(conn.connection.dbapi_connection, error)
            and replace_connection(context, error, "tenant_sync")
        ):
            raise
        replaced = True
    finally:
        raw.close()
    if replaced:
        raw = conn.connection.cursor()
        try:
            raw.execute(sql, params)
        finally:
            raw.close()
    _info(conn.connection)[_STATE_KEY] = tenant
    return replaced


def _statement_tables(context: Any) -> frozenset[str] | None:
    """Tables the statement references (ORM eager loads included), or None
    for raw SQL whose tables cannot be known. Cached per compiled form."""
    compiled = context.compiled
    if compiled is None or isinstance(compiled.statement, TextClause):
        return None
    cached: frozenset[str] | None = getattr(compiled, _TABLES_ATTR, None)
    if cached is not None:
        return cached
    roots = [compiled.statement]
    compile_state = getattr(compiled, "compile_state", None)
    if compile_state is not None and compile_state.statement is not None:
        roots.append(compile_state.statement)  # the ORM-expanded statement
    names = frozenset(
        node.name
        for root in roots
        for node in visitors.iterate(root)
        if isinstance(node, TableClause)
    )
    try:
        setattr(compiled, _TABLES_ATTR, names)
    except AttributeError:  # pragma: no cover - a slotted compiler: no cache
        pass
    return names


#: Violation messages already logged (bounded; the metric counts every one).
_REPORTED: set[str] = set()


def _strict_non_tenant() -> bool:
    """Refuse (raise) a scope violation? Everywhere except production."""

    return not settings.is_prod


def _non_tenant_allowed(context: Any) -> bool:
    """May this statement skip the tenant sync inside the non-tenant scope?"""
    if context.execution_options.get(NON_TENANT_OPTION, False) is True:
        return True
    tables = _statement_tables(context)
    if tables is not None and tables <= NON_TENANT_TABLES:
        return True
    what = (
        "raw SQL (tag it with execution_options(sandloom_non_tenant=True) only if it "
        "touches no tenant table)"
        if tables is None
        else f"table(s) {sorted(tables - NON_TENANT_TABLES)}"
    )
    message = (
        f"tenant_rls: non_tenant_statements() scope ran {what}; only "
        f"{sorted(NON_TENANT_TABLES)} are allowlisted"
    )
    if _strict_non_tenant():
        raise NonTenantStatementError(message)

    _metrics.tenant_rls_non_tenant_violations_total.inc()
    if message not in _REPORTED and len(_REPORTED) < 256:  # warn once per kind
        _REPORTED.add(message)
        logger.warning(
            message + "; the tenant setting is synced for it (fail-safe)",
            extra={"event": "tenant_rls.non_tenant_violation"},
        )
    return False


def _forget(context: Any) -> None:
    _info(context.root_connection.connection).pop(_STATE_KEY, None)


def _wrap_execute(
    original: Callable[..., Any], merged_begin: bool = False
) -> Callable[..., Any]:
    def do_execute(
        cursor: Any, statement: Any, parameters: Any, context: Any = None
    ) -> Any:
        if _sync(context, merged_begin):
            cursor = context.cursor  # the connection was replaced
        try:
            return original(cursor, statement, parameters, context)
        finally:
            if _may_revert(context, statement):
                _forget(context)

    return do_execute


def _wrap_execute_no_params(
    original: Callable[..., Any], merged_begin: bool = False
) -> Callable[..., Any]:
    def do_execute_no_params(cursor: Any, statement: Any, context: Any = None) -> Any:
        if _sync(context, merged_begin):
            cursor = context.cursor  # the connection was replaced
        try:
            return original(cursor, statement, context)
        finally:
            if _may_revert(context, statement):
                _forget(context)

    return do_execute_no_params


def _wrap_tx_boundary(original: Callable[..., Any]) -> Callable[..., Any]:
    """``do_begin`` / ``do_commit`` / ``do_rollback(dbapi_connection)``: a
    transaction starts or ends → its setting is unknown / gone."""

    def wrapper(dbapi_connection: Any) -> Any:
        info = _info(dbapi_connection)
        info.pop(_STATE_KEY, None)
        info.pop(_WARNED_KEY, None)
        return original(dbapi_connection)

    return wrapper


def _set_config_sql(paramstyle: str, tenant: str) -> tuple[str, Any]:
    """Build the parameterised ``set_config`` for the driver's paramstyle.

    A bound parameter (not an inlined literal) keeps ONE prepared statement per
    connection instead of one per tenant id.
    """
    if paramstyle == "pyformat":
        mark: str = "%(t)s"
        params: Any = {"t": tenant}
    elif paramstyle == "named":
        mark, params = ":t", {"t": tenant}
    else:
        mark = {
            "numeric_dollar": "$1",
            "numeric": ":1",
            "qmark": "?",
            "format": "%s",
        }.get(paramstyle, "%s")
        params = (tenant,)
    return f"SELECT set_config('{TENANT_SETTING}', {mark}, true)", params


def install_tenant_rls(
    engine: AsyncEngine | Engine,
    *,
    system: bool = False,
    merged_begin: bool | None = None,
) -> None:
    """Install the RLS setting hook on ``engine`` (idempotent, PostgreSQL only).

    ``system=True`` marks a system-channel engine (its sessions run inside
    ``unscoped()`` by design, so the misuse warning is suppressed).
    ``merged_begin`` (default: ``DB_RLS_MERGED_BEGIN``, on) enables the
    one-message ``BEGIN; set_config`` on asyncpg (see the module docstring).
    """
    if merged_begin is None:
        merged_begin = settings.db_rls_merged_begin
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if system:
        setattr(sync_engine, _SYSTEM_ATTR, True)
    if getattr(sync_engine, _INSTALLED_ATTR, False):
        return
    if sync_engine.dialect.name != "postgresql":
        raise TenantRLSConfigError(
            f"tenant_rls requires PostgreSQL, but this database uses "
            f"{sync_engine.dialect.name!r}; remove the tenant_rls option"
        )
    dialect = sync_engine.dialect
    # The probe runs once per process; on changed driver internals it warns,
    # counts and leaves the plain (separate BEGIN + set_config) path.
    merged = (
        bool(merged_begin) and dialect.driver == "asyncpg" and asyncpg_fast_paths_ok()
    )
    dialect.do_execute = _wrap_execute(dialect.do_execute, merged)  # type: ignore[method-assign]
    # executemany has the same (cursor, statement, parameters, context) shape.
    dialect.do_executemany = _wrap_execute(dialect.do_executemany, merged)  # type: ignore[method-assign]
    dialect.do_execute_no_params = _wrap_execute_no_params(  # type: ignore[method-assign]
        dialect.do_execute_no_params, merged
    )
    # Transaction boundaries (also the pool's reset-on-return rollback).
    dialect.do_begin = _wrap_tx_boundary(dialect.do_begin)  # type: ignore[method-assign]
    dialect.do_commit = _wrap_tx_boundary(dialect.do_commit)  # type: ignore[method-assign]
    dialect.do_rollback = _wrap_tx_boundary(dialect.do_rollback)  # type: ignore[method-assign]
    setattr(sync_engine, _INSTALLED_ATTR, True)


def is_tenant_rls_installed(engine: AsyncEngine | Engine) -> bool:
    """True if :func:`install_tenant_rls` ran on ``engine``."""
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    return bool(getattr(sync_engine, _INSTALLED_ATTR, False))


# --- startup verification -------------------------------------------------

_ROLE_ATTRS = text(
    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
    "WHERE rolname = current_user"
)
#: Superuser / BYPASSRLS roles the current role could ``SET ROLE`` to.
_PRIVILEGED_MEMBERSHIPS = text(
    "SELECT rolname FROM pg_roles WHERE (rolsuper OR rolbypassrls) "
    "AND rolname <> current_user AND pg_has_role(current_user, oid, 'MEMBER') "
    "ORDER BY rolname"
)
#: RLS tables the current role owns (directly or via membership) — an owner can
#: ALTER TABLE ... DISABLE/NO FORCE ROW LEVEL SECURITY or drop the policy.
_OWNED_RLS_TABLES = text(
    "SELECT n.nspname || '.' || c.relname FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relrowsecurity AND pg_has_role(current_user, c.relowner, 'MEMBER') "
    "ORDER BY 1"
)
#: Unconditional (``USING (true)``) policies granted to a role the current role
#: belongs to — e.g. the system-channel policies.
_UNCONDITIONAL_POLICIES = text(
    "SELECT DISTINCT p.schemaname || '.' || p.tablename || ':' || p.policyname "
    "FROM pg_policies p, unnest(p.roles) AS r(role) "
    "WHERE r.role <> 'public' AND pg_has_role(current_user, r.role, 'MEMBER') "
    "AND (p.qual = 'true' OR p.with_check = 'true') ORDER BY 1"
)


async def verify_tenant_rls_roles(
    conn: AsyncConnection, *, system: bool = False
) -> None:
    """Refuse a role that could step outside the tenant policies.

    For the APPLICATION role (``system=False``) every escape hatch is an error:
    superuser, ``BYPASSRLS``, membership in such a role, owning an RLS table,
    and membership in a role with an unconditional policy (the system role).
    For the SYSTEM role
    only superuser / ``BYPASSRLS`` are refused (least privilege: it reaches
    other tenants solely through the per-table ``system_access`` policies).
    Raises :class:`TenantRLSConfigError` listing every problem.
    """
    who = "system-channel" if system else "application"
    row = (await conn.execute(_ROLE_ATTRS)).one()
    user, is_super, bypass = str(row[0]), bool(row[1]), bool(row[2])
    problems: list[str] = []
    if is_super:
        problems.append(f"role {user!r} is a SUPERUSER (ignores every policy)")
    if bypass:
        problems.append(f"role {user!r} has BYPASSRLS")
    privileged = [r[0] for r in await conn.execute(_PRIVILEGED_MEMBERSHIPS)]
    if privileged:
        problems.append(
            f"role {user!r} is a member of superuser/BYPASSRLS role(s) "
            f"{privileged} (SET ROLE escapes RLS)"
        )
    if not system:
        owned = [r[0] for r in await conn.execute(_OWNED_RLS_TABLES)]
        if owned:
            problems.append(
                f"role {user!r} owns RLS table(s) {owned} (an owner can disable "
                "RLS); create tables with a separate migration role"
            )
        unconditional = [r[0] for r in await conn.execute(_UNCONDITIONAL_POLICIES)]
        if unconditional:
            problems.append(
                f"role {user!r} is covered by unconditional policies "
                f"{unconditional} (it is, or belongs to, the system role)"
            )
    if problems:
        raise TenantRLSConfigError(
            f"tenant_rls: the {who} role cannot be confined by Row-Level "
            "Security: " + "; ".join(problems)
        )


_RLS_TABLES = text(
    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') AND c.relname = ANY(:names) "
    "AND n.nspname = ANY(current_schemas(false))"
)
_RLS_POLICIES = text(
    "SELECT tablename, policyname FROM pg_policies "
    "WHERE tablename = ANY(:names) AND schemaname = ANY(current_schemas(false))"
)


async def tenant_rls_coverage(
    conn: AsyncConnection, tables: list[str]
) -> tuple[list[str], list[str]]:
    """``(present, problems)`` of tenant RLS on ``tables`` in this database.

    ``present``: the tables that exist here (a split deployment hosts only its
    own modules' tables). For each, RLS must be ENABLED and FORCED and the
    tenant-isolation policy (``enable_tenant_rls``) must exist.
    """
    rows = (await conn.execute(_RLS_TABLES, {"names": tables})).all()
    policies: dict[str, set[str]] = {}
    for table, policy in (await conn.execute(_RLS_POLICIES, {"names": tables})).all():
        policies.setdefault(str(table), set()).add(str(policy))
    present: list[str] = []
    problems: list[str] = []
    for name, enabled, forced in rows:
        table = str(name)
        present.append(table)
        missing: list[str] = []
        if not enabled:
            missing.append("ROW LEVEL SECURITY not enabled")
        if not forced:
            missing.append("not FORCEd (the owner bypasses it)")
        if tenant_rls_policy_name(table) not in policies.get(table, set()):
            missing.append(f"policy {tenant_rls_policy_name(table)!r} missing")
        if missing:
            problems.append(
                f"{table}: {', '.join(missing)} — add a module migration calling "
                f"sandloom.core.orm.enable_tenant_rls(op, {table!r})"
            )
    return present, problems


__all__ = [
    "NON_TENANT_OPTION",
    "NON_TENANT_TABLES",
    "NonTenantStatementError",
    "TenantRLSConfigError",
    "install_tenant_rls",
    "is_tenant_rls_installed",
    "non_tenant_statements",
    "tenant_rls_coverage",
    "verify_tenant_rls_roles",
]
