"""Stripe webhook idempotency journal."""

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt

EVENT_STATUS_PROCESSED = "processed"
EVENT_STATUS_SKIPPED = "skipped"
EVENT_STATUS_FAILED = "failed"


class StripeEvent(Base):
    """Processed PSP webhook events (unique event_id ⇒ at-most-once apply)."""

    __tablename__ = "billing_stripe_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    received_at: Mapped[CreatedAt]
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
