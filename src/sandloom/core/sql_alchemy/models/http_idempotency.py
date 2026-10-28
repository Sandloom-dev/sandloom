"""Durable HTTP idempotency log (``idempotency_guard(durable=True)``).

One row per ``Idempotency-Key`` a durable guard accepted, keyed by a digest of
``(route scope, caller, tenant, key)``. The row is claimed in the REQUEST's own
Unit of Work before the endpoint runs and completed with the response (status,
headers, body) right before ``COMMIT``, so the record and the endpoint's effect
commit — or roll back — together (ADR-0005, the third adapter of the durable
dedupe record). A retry finds the committed row and gets the stored response
back instead of re-running the endpoint; a rolled-back request leaves no row.

Retention: rows expire after the guard's ``ttl_seconds`` (``expires_at``,
24 h by default); an expired row is taken over by the next request with its
key and purged by the outbox relay's housekeeping / ``sandloom outbox purge``.

Tenant isolation is by key construction, not Row-Level Security: like the
other core infrastructure tables (``outbox_messages``, ``contract_command_log``)
the table carries no tenant policy, because a durable guard runs before the
route may have bound a tenant and on user-scoped routes that never bind one.
Every read is by the full digest, which includes the caller and the tenant.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


def _utc_naive_now() -> datetime:  # pragma: no cover - trivial
    return datetime.now(UTC).replace(tzinfo=None)


class HttpIdempotencyRecord(Base):
    __tablename__ = "http_idempotency_log"
    __table_args__ = (Index("ix_http_idempotency_log_expires_at", "expires_at"),)

    # sha256 hex of (route scope, caller, tenant, Idempotency-Key).
    dedupe_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The guard's ``scope`` (diagnostics, extraction; part of the digest).
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Request fingerprint (method, path, query, caller, tenant, body):
    # same key + other fingerprint -> 422.
    args_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # "pending" (only ever visible inside the claiming transaction),
    # "succeeded" (``result`` replayable) or "not_replayable" (status only).
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # The stored response (JSON: status, headers, body, media type).
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_naive_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
