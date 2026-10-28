"""projection_watermarks: the tombstone purge watermark of a projection

One row per projection: ``purged_before`` (monotonic; the apply path
dead-letters an event emitted before it, so an operator replay cannot
resurrect a purged row) and ``rebuild_started_at`` (a purge refuses while a
rebuild runs). Spec §6.4 item 5. Additive: a new table, nothing else changes.

Revision ID: 0011_projection_watermarks
Revises: 0010_saga_state
Create Date: 2026-10-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_projection_watermarks"
down_revision: str | Sequence[str] | None = "0010_saga_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "projection_watermarks",
        sa.Column("projection", sa.String(length=200), nullable=False),
        sa.Column("purged_before", sa.DateTime(), nullable=True),
        sa.Column("rebuild_started_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("projection"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("projection_watermarks")
