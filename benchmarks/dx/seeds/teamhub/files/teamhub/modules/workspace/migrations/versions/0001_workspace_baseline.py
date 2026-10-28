"""workspace baseline: the settings table

Revision ID: 0001_workspace
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

revision: str = "0001_workspace"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_settings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),  # also the tenant_id index
    )
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "workspace_settings")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "workspace_settings")
    op.drop_table("workspace_settings")
