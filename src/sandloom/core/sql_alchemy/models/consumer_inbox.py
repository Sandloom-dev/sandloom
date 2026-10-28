"""Durable consumer-side inbox (exactly-once *effect* for critical handlers).

At-least-once delivery (post-commit dispatch + relay + transport redelivery)
means the same stable envelope id can arrive more than once. The cache-backed
:class:`sandloom.core.eventing.consumer_inbox.ConsumerInbox` deduplicates within a
TTL window but is fail-open and ephemeral — fine for idempotent / non-critical
handlers, NOT a guarantee for money / orders / provisioning.

This table is the durable alternative. A handler claims an event by inserting a
``UNIQUE(consumer, event_id)`` row inside its OWN Unit of Work, in the same
transaction as the side effect: if the transaction commits, the effect and the
claim commit together; if a duplicate delivery arrives, the unique constraint
rejects the second claim and the handler skips the effect. There is no window
in which the effect runs but the claim is lost.

Retention: this table grows one row per processed event and has NO TTL. Operators
should periodically prune old claims via the ``purge_processed_before`` method of
:class:`sandloom.core.sql_alchemy.durable_inbox.DurableConsumerInbox`, with a cutoff
comfortably past the redelivery window so a late duplicate still finds its claim.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


def _utc_naive_now() -> datetime:  # pragma: no cover - trivial
    return datetime.now(UTC).replace(tzinfo=None)


class ConsumerInboxEntry(Base):
    __tablename__ = "consumer_inbox"
    __table_args__ = (
        UniqueConstraint(
            "consumer", "event_id", name="uq_consumer_inbox_consumer_event"
        ),
        # Retention purge (``purge_processed_before``): without it every purge
        # call scans the whole table, even when nothing is old enough.
        Index("ix_consumer_inbox_processed_at", "processed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Logical consumer identity, e.g. "billing.credits@orders.paid.v1".
    consumer: Mapped[str] = mapped_column(String(200), nullable=False)
    # Stable domain event id (== outbox row id == envelope id).
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="processed", nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        default=_utc_naive_now, nullable=False
    )
