"""Durable command-contract idempotency log (exactly-once command *effect*).

One row per completed command contract call, keyed by a digest of
``(contract.method, bound tenant, idempotency key)``. The row is claimed in the
command's OWN Unit of Work before the body runs and completed with the encoded
result before the commit, so the record and the business change commit — or
roll back — together. A retry finds the committed row and replays the stored
result instead of re-executing; there is no window in which the effect
committed but the record was lost (the old cache-only dedupe had one).

Retention: rows expire after ``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS``
(``expires_at``); an expired row is taken over by the next call with its key
and purged by the outbox relay's housekeeping / ``sandloom outbox purge``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


def _utc_naive_now() -> datetime:  # pragma: no cover - trivial
    return datetime.now(UTC).replace(tzinfo=None)


class ContractCommandRecord(Base):
    __tablename__ = "contract_command_log"
    __table_args__ = (Index("ix_contract_command_log_expires_at", "expires_at"),)

    # sha256 hex of (contract.method, bound tenant, idempotency key).
    dedupe_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # "<contract>.<method>" (diagnostics; part of the digest).
    contract: Mapped[str] = mapped_column(String(300), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Canonical digest of the call's arguments (same key + other args -> 409).
    args_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    # "pending" (only ever visible inside the claiming transaction),
    # "succeeded" (``result`` replayable) or "not_replayable" (too large).
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # The result's contract-wire form, JSON-encoded.
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the execution left on its caller's request (JSON call effects:
    # risks, remote calls in doubt, self-revocation, pending step-ups): a
    # replay re-applies them, so it marks the caller like the first run.
    effects: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_naive_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
