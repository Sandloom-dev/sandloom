"""outbox relay retry backoff

Adds ``outbox_messages.next_attempt_at`` so the relay defers a failed row with
exponential backoff + jitter instead of retrying it on every tick (which, during
a broker outage, burned every pending row's attempt budget within seconds and
dead-lettered committed events en masse). NULL means "due now".

Revision ID: 0004_outbox_backoff
Revises: 0003_outbox_claim_token
Create Date: 2026-10-08 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_outbox_backoff"
down_revision: str | Sequence[str] | None = "0003_outbox_claim_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "outbox_messages",
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("outbox_messages", "next_attempt_at")
