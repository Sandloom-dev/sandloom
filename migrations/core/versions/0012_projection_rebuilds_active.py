"""projection_watermarks.rebuilds_active: count running rebuilds

A purge refuses while ANY rebuild of the projection runs: the marker counts
running rebuilds and ``rebuild_started_at`` stays set until the last one
ends (two concurrent rebuilds no longer share one timestamp the first to
finish clears). G-MSTORE-09. Additive: a new column with a default.

Revision ID: 0012_projection_rebuilds_active
Revises: 0011_projection_watermarks
Create Date: 2026-10-26 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_projection_rebuilds_active"
down_revision: str | Sequence[str] | None = "0011_projection_watermarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projection_watermarks",
        sa.Column(
            "rebuilds_active",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    # a rebuild marked running before the upgrade counts as one
    op.execute(
        "UPDATE projection_watermarks SET rebuilds_active = 1 "
        "WHERE rebuild_started_at IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projection_watermarks", "rebuilds_active")
