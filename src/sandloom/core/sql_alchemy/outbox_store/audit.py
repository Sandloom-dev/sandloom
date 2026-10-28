"""The relational :class:`~sandloom.core.infra.contracts.AuditStore` (``audit_log``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select

from sandloom.core.infra.contracts import (
    AuditStore,
)
from sandloom.core.sql_alchemy import effect_rows
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.outbox_store.statements import (
    _naive_utc,
)

_AUDIT_FIELDS = (
    "id",
    "occurred_at",
    "actor_id",
    "actor_type",
    "action",
    "resource_type",
    "resource_id",
    "request_id",
    "trace_id",
)


def _audit_row(row: Any) -> dict[str, Any]:
    """Sanitized audit metadata; free-form ``data`` is never published."""
    item = {name: getattr(row, name) for name in _AUDIT_FIELDS}
    # The legacy audit column stores naive UTC. Publish an explicit offset so an
    # operator's browser cannot interpret it as local time.
    occurred = row.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    item["occurred_at"] = occurred
    return item


class SQLAlchemyAuditStore(AuditStore):
    """Audit log over the ``audit_log`` table."""

    async def query(
        self,
        session: Any,
        *,
        skip: int,
        limit: int,
        action: str | None,
        actor_id: uuid.UUID | None,
        resource_id: str | None,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditLogEntry)
        if action is not None:
            stmt = stmt.where(AuditLogEntry.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLogEntry.actor_id == actor_id)
        if resource_id is not None:
            stmt = stmt.where(AuditLogEntry.resource_id == resource_id)
        rows = await session.scalars(
            stmt.order_by(AuditLogEntry.occurred_at.desc(), AuditLogEntry.id)
            .offset(skip)
            .limit(limit)
        )
        return [_audit_row(row) for row in rows]

    async def query_page(
        self,
        session: Any,
        *,
        limit: int,
        before: tuple[datetime, uuid.UUID] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        action: str | None = None,
        action_prefix: str | None = None,
        actor_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditLogEntry)
        if action is not None:
            stmt = stmt.where(AuditLogEntry.action == action)
        if action_prefix:
            escaped = (
                action_prefix.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            stmt = stmt.where(AuditLogEntry.action.like(f"{escaped}%", escape="\\"))
        if actor_id is not None:
            stmt = stmt.where(AuditLogEntry.actor_id == actor_id)
        if resource_type is not None:
            stmt = stmt.where(AuditLogEntry.resource_type == resource_type)
        if resource_id is not None:
            stmt = stmt.where(AuditLogEntry.resource_id == resource_id)
        # The column stores naive UTC: compare in the same convention.
        if since is not None:
            stmt = stmt.where(AuditLogEntry.occurred_at >= _naive_utc(since))
        if until is not None:
            stmt = stmt.where(AuditLogEntry.occurred_at < _naive_utc(until))
        if before is not None:
            at, row_id = _naive_utc(before[0]), before[1]
            stmt = stmt.where(
                or_(
                    AuditLogEntry.occurred_at < at,
                    and_(AuditLogEntry.occurred_at == at, AuditLogEntry.id < row_id),
                )
            )
        newest_first = (AuditLogEntry.occurred_at.desc(), AuditLogEntry.id.desc())
        rows = await session.scalars(stmt.order_by(*newest_first).limit(limit))
        return [_audit_row(row) for row in rows]

    def record(
        self,
        session: Any,
        *,
        action: str,
        actor_id: uuid.UUID | None,
        actor_type: str,
        resource_type: str | None,
        resource_id: str | None,
        data: dict[str, Any] | None,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        effect_rows.register(
            session,
            effect_rows.AuditRow(
                action,
                actor_id,
                actor_type,
                resource_type,
                resource_id,
                data,
                request_id,
                trace_id,
            ),
        )
