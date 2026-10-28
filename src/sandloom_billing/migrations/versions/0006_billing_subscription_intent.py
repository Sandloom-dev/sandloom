"""billing_subscriptions.intent_id: one subscription per checkout intent (ADR-0003)

Revision ID: 0006_billing
Revises: 0005_billing
Create Date: 2026-10-12 17:00:00

The checkout intent a subscription was created for (``origin = 'intent'``).
UNIQUE when set (partial index): one intent yields at most one subscription
(open item 16) — two concurrent webhook deliveries for different
subscriptions of one intent cannot both insert. Existing rows stay NULL
(unconstrained).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_billing"
down_revision: str | Sequence[str] | None = "0005_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "billing_subscriptions", sa.Column("intent_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        "uq_billing_subscriptions_intent_id",
        "billing_subscriptions",
        ["intent_id"],
        unique=True,
        postgresql_where=sa.text("intent_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_billing_subscriptions_intent_id", table_name="billing_subscriptions"
    )
    op.drop_column("billing_subscriptions", "intent_id")
