"""Transactional Outbox SQLAlchemy model.

This table stores domain events to be reliably relayed to the event bus
outside of the DB transaction (Outbox pattern).

NOTE:
The underlying table uses ``TIMESTAMP WITHOUT TIME ZONE`` columns for the
timestamp fields. We therefore must persist *naive* UTC datetimes. A previous
implementation attempted to use a ``@staticmethod`` as the default factory;
SQLAlchemy received the ``staticmethod`` descriptor instead of the underlying
callable, falling back to assigning a value earlier in the lifecycle that was
timezone-aware (``datetime(..., tzinfo=UTC)``). That led to asyncpg raising
``can't subtract offset-naive and offset-aware datetimes`` when binding the
parameter. We now expose a simple module-level callable returning a naive
UTC ``datetime`` to guarantee correct binding.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


def _utc_naive_now() -> datetime:  # pragma: no cover - trivial
    """Return naive UTC ``datetime`` suitable for a TIMESTAMP WITHOUT TZ column.

    We obtain an aware UTC timestamp then strip tzinfo to avoid driver warnings
    and maintain consistency with the column type.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    # The ORM model is the CANONICAL schema: alembic migrations AND
    # ``migrations.ensure_core_infra_tables`` (which does ``Table.create`` from
    # this model on secondary databases) must produce the SAME table — columns
    # AND indexes. These index definitions mirror the alembic chain exactly
    # (0001_core_infra; the locked_until lease column is added in
    # 0002_outbox_lease, locked_by in 0003, next_attempt_at in 0004; 0005
    # replaced the full pending/channel indexes with the partial dead/delivered
    # ones); keep them in sync if either changes.
    #
    # Every index is PARTIAL and serves one real query, so a pending row costs
    # entries only in the PK and the relay index:
    # * relay — the relay's claim (due rows, oldest first);
    # * dead — list / requeue dead-lettered rows (``sandloom outbox``);
    # * delivered — the retention purge (``delivered_at < cutoff``).
    __table_args__ = (
        Index(
            "ix_outbox_messages_relay",
            "emitted_at",
            postgresql_where=text("delivered_at IS NULL AND dead_at IS NULL"),
        ),
        Index(
            "ix_outbox_messages_dead",
            "dead_at",
            postgresql_where=text("dead_at IS NOT NULL"),
        ),
        Index(
            "ix_outbox_messages_delivered",
            "delivered_at",
            postgresql_where=text("delivered_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(10), default="v1", nullable=False
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Naive UTC timestamp for emitted time
    emitted_at: Mapped[datetime] = mapped_column(default=_utc_naive_now, nullable=False)
    # Delivery status fields
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Terminal dead-letter state: set when delivery_attempts crosses the relay's
    # max-attempts budget. A dead row is never retried (it drops out of the
    # relay's pending query) and is surfaced for operator action, so a single
    # poison event neither retries forever nor silently rots as undelivered.
    dead_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Lease held by the relay worker currently publishing this row. The relay
    # CLAIMS a row (sets locked_until = now + lease) and COMMITS before doing
    # broker I/O, so no DB row lock is held during the publish. The pending query
    # skips a row whose lease is still in the future; a crashed worker's claim
    # self-heals once its lease expires and another worker reclaims the row.
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    # Owner token of the current lease. Each claim mints a fresh UUID; settle
    # only acts WHERE locked_by matches, so a worker whose lease EXPIRED (and was
    # reclaimed by another worker) cannot clear or mutate the new owner's claim
    # when its own late publish finally returns.
    locked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Earliest time the relay may retry this row. Set on every failed attempt
    # (exponential backoff + jitter) and on a broker-unavailable deferral, so a
    # failing row is NOT re-hammered every relay tick and its attempt budget is
    # spread over time instead of being exhausted within seconds. NULL = due now
    # (subject to the emitted_at grace for fresh rows). Added in
    # 0004_outbox_backoff.
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def mark_attempt(self, error: str | None = None) -> None:
        self.delivery_attempts += 1
        self.last_error = error

    def mark_delivered(self) -> None:
        # Maintain naive UTC consistency
        self.delivered_at = datetime.now(UTC).replace(tzinfo=None)
        self.last_error = None

    def mark_dead(self, error: str | None = None) -> None:
        """Move the message to the terminal dead-letter state."""
        self.dead_at = datetime.now(UTC).replace(tzinfo=None)
        if error is not None:
            self.last_error = error
