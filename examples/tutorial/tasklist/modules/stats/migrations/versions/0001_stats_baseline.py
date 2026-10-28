"""stats baseline: the item counters (rendered by `sandloom add module`, then edited
before the first deploy)

Revision ID: 0001_stats
Revises:
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision: str = "0001_stats"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --8<-- [start:upgrade]
def upgrade() -> None:
    op.create_table(
        "stats_item_counts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "stats_item_counts")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "stats_item_counts")
    op.drop_table("stats_item_counts")


# --8<-- [end:upgrade]
