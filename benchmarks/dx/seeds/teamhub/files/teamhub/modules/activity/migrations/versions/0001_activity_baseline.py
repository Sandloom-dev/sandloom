"""activity baseline: the feed table

Revision ID: 0001_activity
Revises:
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sandloom.sdk.data import (  # sandloom: advanced RLS
    disable_tenant_rls,
    enable_tenant_rls,
)

revision: str = "0001_activity"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activity_entries",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_activity_entries_tenant_id"), "activity_entries", ["tenant_id"]
    )
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "activity_entries")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "activity_entries")
    op.drop_index(op.f("ix_activity_entries_tenant_id"), table_name="activity_entries")
    op.drop_table("activity_entries")
