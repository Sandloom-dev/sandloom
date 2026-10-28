"""outbox relay claim owner token

Adds ``outbox_messages.locked_by`` so the two-phase relay can verify lease
ownership on settle. A worker whose lease expired and was reclaimed by another
worker must not clear or mutate the new owner's claim when its own late publish
returns — settle only acts WHERE locked_by matches the claiming token.

Revision ID: 0003_outbox_claim_token
Revises: 0002_outbox_lease
Create Date: 2026-06-15 03:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_outbox_claim_token"
down_revision: str | Sequence[str] | None = "0002_outbox_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "outbox_messages",
        sa.Column("locked_by", sa.UUID(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("outbox_messages", "locked_by")
