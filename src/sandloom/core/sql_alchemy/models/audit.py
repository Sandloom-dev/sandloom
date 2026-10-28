"""Append-only audit log model (cross-cutting, like the outbox).

Records who did what to which resource and when, for security-sensitive
operations (role changes, deletions, privilege grants, …). Entries are written
inside the request's Unit of Work, so an audit row commits if and only if the
audited change commits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


def _utc_naive_now() -> datetime:  # pragma: no cover - trivial
    return datetime.now(UTC).replace(tzinfo=None)


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    occurred_at: Mapped[datetime] = mapped_column(
        default=_utc_naive_now, nullable=False, index=True
    )
    # Who performed the action (UUID of the actor; null for system actions).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    actor_type: Mapped[str] = mapped_column(String(50), default="user", nullable=False)
    # What happened, versioned dotted action (e.g. "tenancy.member.role_changed").
    action: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    # The affected resource (referenced by plain type+id, no cross-module FK).
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )
    # Structured context (before/after, reason, …).
    data: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    # Correlation with the originating request/trace.
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
