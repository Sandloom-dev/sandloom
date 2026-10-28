"""tasks baseline: the example table (rendered by `sandloom add module`)

Revision ID: 0001_tasks
Revises:
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision: str = "0001_tasks"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks_items",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tasks_items_tenant_id"), "tasks_items", ["tenant_id"])
    op.create_index(op.f("ix_tasks_items_owner_id"), "tasks_items", ["owner_id"])
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "tasks_items")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "tasks_items")
    op.drop_index(op.f("ix_tasks_items_owner_id"), table_name="tasks_items")
    op.drop_index(op.f("ix_tasks_items_tenant_id"), table_name="tasks_items")
    op.drop_table("tasks_items")
