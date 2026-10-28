"""outbox: partial indexes that serve the real queries + retention purge

Replaces the two FULL indexes nothing reads efficiently:

* ``ix_outbox_messages_pending (delivered_at, delivery_attempts)`` — the relay
  claims through the partial ``ix_outbox_messages_relay (emitted_at) WHERE
  delivered_at IS NULL AND dead_at IS NULL``; the full index only cost an
  entry per INSERT and another per mark-delivered UPDATE;
* ``ix_outbox_messages_channel (channel)`` — only the dead-letter tools filter
  by channel, always together with ``dead_at IS NOT NULL``.

New:

* ``ix_outbox_messages_dead (dead_at) WHERE dead_at IS NOT NULL`` — list /
  requeue dead-lettered rows (ordered by ``dead_at``; channel filtered on the
  small dead set);
* ``ix_outbox_messages_delivered (delivered_at) WHERE delivered_at IS NOT
  NULL`` — the retention purge (``DELETE … WHERE delivered_at < cutoff``),
  which keeps the table (and every index) bounded by OUTBOX_RETENTION_HOURS.

A pending row now carries index entries only in the PK and the relay index.

Revision ID: 0005_outbox_partial_indexes
Revises: 0004_outbox_backoff
Create Date: 2026-10-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_outbox_partial_indexes"
down_revision: str | Sequence[str] | None = "0004_outbox_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_outbox_messages_pending", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_channel", table_name="outbox_messages")
    op.create_index(
        "ix_outbox_messages_dead",
        "outbox_messages",
        ["dead_at"],
        unique=False,
        postgresql_where=sa.text("dead_at IS NOT NULL"),
    )
    op.create_index(
        "ix_outbox_messages_delivered",
        "outbox_messages",
        ["delivered_at"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_outbox_messages_delivered", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_dead", table_name="outbox_messages")
    op.create_index(
        "ix_outbox_messages_channel", "outbox_messages", ["channel"], unique=False
    )
    op.create_index(
        "ix_outbox_messages_pending",
        "outbox_messages",
        ["delivered_at", "delivery_attempts"],
        unique=False,
    )
