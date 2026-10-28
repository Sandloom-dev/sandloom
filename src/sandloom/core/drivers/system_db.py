"""System channel: audited, privileged database sessions (kernel-internal).

With ``tenant_rls`` the ordinary application role has NO way to bypass
Row-Level Security — the tenant policy has no bypass branch and ``unscoped()``
only lifts the ORM guard. Cross-tenant work that legitimately needs every
tenant's rows (ops tooling, repairs, framework maintenance jobs) runs here
instead: on a SEPARATE engine logged in as a SEPARATE least-privilege role
(default ``sandloom_system``) that reaches other tenants only on tables migrated with
``enable_tenant_rls(op, table, system_access=True)``.

Boundaries:

* **Not reachable by business modules.** It lives in the driver tree
  (``sandloom.core.drivers``): the ``infra-imports`` rule rejects a static OR
  dynamic import of it in a module, and it is not in the public core API
  (``public_api`` in ``architecture_rules.yml``). The engine is never on the
  provider, DbSession, ``db_session()``,
  ``Database(name)`` or any capability. This is an architecture boundary inside
  one Python process, NOT a sandbox: code with arbitrary Python execution in the
  process can reach anything the process holds.
* **Authorized + audited.** Every session needs a non-empty ``actor`` and a
  ``reason``; it logs a structured start/end record (``sandloom.system_db``) with
  actor, reason, database, tenant, operation id and outcome, and writes an
  ``sandloom.system_db.session`` row to ``audit_log`` in the SAME transaction (the
  system role needs ``INSERT`` on ``audit_log``) — no audit row, no commit.
* **Its own transaction.** The session is not the ambient request/worker UoW:
  ``publish_event`` / ``record_audit`` inside it do not land in it. Commit on
  success, rollback on any exception.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.drivers.sqlalchemy_database import (
    SystemChannelUnavailable,
    get_system_engine,
    system_channel_configured,
)
from sandloom.core.infra.config import DEFAULT_INSTANCE
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_audit_store
from sandloom.core.sql_alchemy.write_admission import KernelSession
from sandloom.core.tenant_context import bind_tenant, unscoped
from sandloom.core.unit_of_work.request import fresh_admission_scope

logger = logging.getLogger("sandloom.system_db")

#: Audit action recorded for every system-channel session.
AUDIT_ACTION = "sandloom.system_db.session"
_MIN_REASON = 8


class SystemSessionRefused(PermissionError):
    """A system session was requested without a valid actor / reason."""


@asynccontextmanager
async def system_session(
    *,
    actor: str,
    reason: str,
    database: str = DEFAULT_INSTANCE,
    tenant_id: uuid.UUID | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open an audited session on ``database``'s system channel.

    ``actor`` names who runs it (an operator id, ``"cli:projection-rebuild"``,
    a job name); ``reason`` says why (≥ 8 chars, logged and audited).
    ``tenant_id`` scopes the session to one tenant (bound for the ORM guard and
    the RLS setting); without it the session runs ``unscoped()`` and reaches
    every tenant on ``system_access`` tables only (other RLS tables: no rows).

    Raises :class:`SystemSessionRefused` for a missing actor/reason and
    :class:`SystemChannelUnavailable` when no system channel is configured.
    """
    actor = (actor or "").strip()
    reason = (reason or "").strip()
    if not actor:
        raise SystemSessionRefused("system_session requires a non-empty actor")
    if len(reason) < _MIN_REASON:
        raise SystemSessionRefused(
            f"system_session requires a reason of at least {_MIN_REASON} chars"
        )
    engine = get_system_engine(database)
    op_id = uuid.uuid4()
    fields = {
        "event": "system_db.session",
        "op_id": str(op_id),
        "actor": actor,
        "reason": reason,
        "database": database,
        "tenant_id": str(tenant_id) if tenant_id else None,
    }
    logger.warning("system channel session start", extra={**fields, "phase": "start"})
    started = time.monotonic()
    outcome = "error"
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=KernelSession,
        expire_on_commit=False,
    )
    with ExitStack() as scope:
        # an audited system operation: its own admission scope (a kernel unit)
        scope.enter_context(fresh_admission_scope())
        if tenant_id is not None:
            scope.enter_context(bind_tenant(tenant_id))
        else:
            scope.enter_context(unscoped())
        try:
            async with sessions() as session, session.begin():
                sql_alchemy_audit_store().record(
                    session,
                    action=AUDIT_ACTION,
                    actor_id=None,
                    actor_type="system",
                    resource_type="database",
                    resource_id=database,
                    data={
                        "op_id": str(op_id),
                        "actor": actor,
                        "reason": reason,
                        "tenant_id": fields["tenant_id"],
                    },
                    request_id=None,
                    trace_id=None,
                )
                yield session
                # Flush the audit row before COMMIT so a missing grant fails
                # the whole privileged operation (no audit, no commit).
                await session.flush()
            outcome = "committed"
        except BaseException:
            outcome = "rolled_back"
            raise
        finally:
            logger.warning(
                "system channel session end",
                extra={
                    **fields,
                    "phase": "end",
                    "outcome": outcome,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )


__all__ = [
    "AUDIT_ACTION",
    "SystemChannelUnavailable",
    "SystemSessionRefused",
    "system_channel_configured",
    "system_session",
]
