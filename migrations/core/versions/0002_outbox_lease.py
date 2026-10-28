"""outbox relay lease column

Adds ``outbox_messages.locked_until`` so the relay can claim a row (and COMMIT)
before doing broker I/O — no DB row lock is held during publish. The pending
query skips a row whose lease is still in the future; a crashed worker's claim
self-heals once its lease expires.

Revision ID: 0002_outbox_lease
Revises: 0001_core_infra
Create Date: 2026-06-15 02:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_outbox_lease"
down_revision: str | Sequence[str] | None = "0001_core_infra"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "outbox_messages",
        sa.Column("locked_until", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("outbox_messages", "locked_until")
