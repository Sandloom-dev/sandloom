"""Preserve the originating trace through durable outbox delivery.

Revision ID: 0008_outbox_trace_id
Revises: 0007_consumer_inbox_processed_at
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_outbox_trace_id"
down_revision: str | Sequence[str] | None = "0007_consumer_inbox_processed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox_messages", sa.Column("trace_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_messages", "trace_id")
