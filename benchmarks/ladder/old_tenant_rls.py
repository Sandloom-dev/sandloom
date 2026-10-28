"""The event-based RLS hook (before_cursor_execute + begin/commit/rollback
listeners) as on fix/rls-tenant-bypass, kept for the ladder's
wrapper-vs-listener rungs (L2Wo). NOT framework code."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.core.sql_alchemy.rls import TENANT_SETTING
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


class TenantRLSConfigError(RuntimeError):
    """``tenant_rls`` was enabled on a database that cannot enforce it.

    Raised for a non-PostgreSQL backend and by :func:`verify_tenant_rls_roles`
    when a role could step outside the policies. A security misconfiguration,
    so startup aborts on it in EVERY environment (``fatal_at_startup``).
    """

    fatal_at_startup = True


def _desired() -> str:
    tid = _current_tenant.get()
    return str(uuid.UUID(str(tid))) if tid is not None else ""


def _reset(conn: Connection, *_args: Any) -> None:
    conn.info.pop(_STATE_KEY, None)


def _reset_txn(conn: Connection, *_args: Any) -> None:
    conn.info.pop(_STATE_KEY, None)
    conn.info.pop(_WARNED_KEY, None)


def _sync(
    conn: Connection,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    desired = _desired()
    if (
        not desired
        and _bypass.get()
        and not conn.info.get(_WARNED_KEY)
        and not getattr(conn.engine, _SYSTEM_ATTR, False)
    ):
        # unscoped() with no tenant on an RLS engine is almost always code that
        # expects cross-tenant access; the database will show it nothing.
        conn.info[_WARNED_KEY] = True
        logger.warning(
            "unscoped() on a tenant_rls database does not lift Row-Level "
            "Security: with no tenant bound, RLS tables return no rows and "
            "refuse writes. Use the system channel "
            "(sandloom.core.drivers.system_db) for cross-tenant work.",
            extra={"event": "tenant_rls.unscoped_without_tenant"},
        )
    if conn.info.get(_STATE_KEY) == desired:
        return
    sql, params = _set_config_sql(conn.dialect.paramstyle, desired)
    # A separate DB-API cursor on the SAME connection/transaction: it neither
    # re-enters these events nor disturbs the cursor about to run ``statement``.
    raw = conn.connection.cursor()
    try:
        raw.execute(sql, params)
    finally:
        raw.close()
    conn.info[_STATE_KEY] = desired


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


def install_tenant_rls(engine: AsyncEngine | Engine, *, system: bool = False) -> None:
    """Install the RLS setting hook on ``engine`` (idempotent, PostgreSQL only).

    ``system=True`` marks a system-channel engine (its sessions run inside
    ``unscoped()`` by design, so the misuse warning is suppressed).
    """
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
    event.listen(sync_engine, "before_cursor_execute", _sync)
    # A new transaction starts with unknown settings (and conn.info outlives the
    # transaction on a pooled connection): force a sync on its first statement.
    event.listen(sync_engine, "begin", _reset_txn)
    event.listen(sync_engine, "commit", _reset_txn)
    event.listen(sync_engine, "rollback", _reset_txn)
    # ROLLBACK TO SAVEPOINT reverts SET LOCAL done inside the savepoint.
    event.listen(sync_engine, "rollback_savepoint", _reset)
    setattr(sync_engine, _INSTALLED_ATTR, True)


def is_tenant_rls_installed(engine: AsyncEngine | Engine) -> bool:
    """True if :func:`install_tenant_rls` ran on ``engine``."""
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    return bool(getattr(sync_engine, _INSTALLED_ATTR, False))
