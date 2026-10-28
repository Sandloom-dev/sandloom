"""Cross-tenant access to saga state (ADR-0008 §2, "Row-level security").

The runtime touches an instance with its tenant bound, so the app role sees
exactly the rows of the instance it drives. Work that spans tenants — the
timer sweep, ``sandloom sagas list`` without ``--tenant`` and every operator
mutation — goes through :func:`cross_tenant_session`:

* on a ``tenant_rls`` database: the kernel's audited **system channel**
  (``system_session(actor=, reason=)``; the saga tables are migrated with
  ``system_access=True``) — never ``unscoped()``, which does not lift RLS;
* otherwise: an ordinary Unit of Work on the database inside ``unscoped()``
  (the saga tables are not ``TenantScoped``; ``unscoped`` only documents the
  intent).

``uow`` on the yielded :class:`CrossTenantSession` says whether the session
is a Unit of Work (``publish_event`` / ``record_audit`` land in it) — the
system session is not: callers publish nudges afterwards in a UoW of their
own and write audit rows through the audit store directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_audit_store
from sandloom.core.tenant_context import unscoped
from sandloom.core.unit_of_work import db_session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CrossTenantSession:
    session: AsyncSession
    #: The session is a Unit of Work (outbox / audit helpers land in it).
    uow: bool


@dataclass(frozen=True, slots=True)
class SystemChannel:
    """The kernel's system channel, injected by the composition root.

    ``open(actor=, reason=, database=)`` is
    ``sandloom.core.drivers.system_db.system_session``; ``configured(database)``
    says whether an instance has one. The saga runtime never imports the
    driver tree (``create_app`` hands it over, ADR-0007).
    """

    open: Callable[..., AbstractAsyncContextManager[Any]]
    configured: Callable[[str], bool]


def tenant_rls_enabled(core: Any, database: str) -> bool:
    """``database`` runs with ``tenant_rls`` (its provider advertises it)."""
    try:
        provider = core.database(database)
    except (KeyError, LookupError, AttributeError):
        return False
    features = getattr(provider, "features", None)
    return callable(features) and "tenant_rls" in features()


def system_channel_problem(
    core: Any, database: str, channel: SystemChannel | None
) -> str | None:
    """Why the sweep could not reach other tenants' instances on ``database``."""
    if not tenant_rls_enabled(core, database):
        return None
    if channel is not None and channel.configured(database):
        return None
    return (
        f"database/{database} runs with tenant_rls but has no system channel: "
        "the saga timer sweep and `sandloom sagas` need it to reach instances "
        "across tenants. Fix: set DATABASE_SYSTEM_URL (or the instance's "
        "system_url) for the sandloom_system role"
    )


class _ReadOnly(Exception):
    """Rolls a read-only cross-tenant session back (no audit row, no writes)."""


@asynccontextmanager
async def cross_tenant_session(
    core: Any,
    database: str,
    *,
    actor: str,
    reason: str,
    channel: SystemChannel | None,
    read_only: bool = False,
) -> AsyncIterator[CrossTenantSession]:
    """A session that reaches every tenant's saga rows on ``database``.

    ``read_only`` rolls it back at the end (``sandloom doctor`` never writes —
    not even the system channel's own audit row).
    """
    try:
        async with _open(core, database, actor, reason, channel) as cts:
            yield cts
            if read_only:
                raise _ReadOnly
    except _ReadOnly:
        return


@asynccontextmanager
async def _open(
    core: Any, database: str, actor: str, reason: str, channel: SystemChannel | None
) -> AsyncIterator[CrossTenantSession]:
    if tenant_rls_enabled(core, database):
        problem = system_channel_problem(core, database, channel)
        if problem is not None or channel is None:
            raise RuntimeError(problem or "no system channel wired")
        async with channel.open(actor=actor, reason=reason, database=database) as s:
            yield CrossTenantSession(s, uow=False)
        return
    with unscoped():
        async with db_session(database) as session:
            yield CrossTenantSession(session, uow=True)


def write_audit(
    session: AsyncSession,
    action: str,
    *,
    actor: str,
    resource_id: str,
    data: dict[str, Any],
) -> None:
    """An operator audit row in ``session``'s transaction (both channels)."""
    sql_alchemy_audit_store().record(
        session,
        action=action,
        actor_id=None,
        actor_type="operator",
        resource_type="saga",
        resource_id=resource_id,
        data={"operator": actor, **data},
        request_id=None,
        trace_id=None,
    )


__all__ = [
    "CrossTenantSession",
    "SystemChannel",
    "cross_tenant_session",
    "system_channel_problem",
    "tenant_rls_enabled",
    "write_audit",
]
