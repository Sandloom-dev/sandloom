"""board baseline: the example table (rendered by `sandloom add module`)

Revision ID: 0001_board
Revises:
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision: str = "0001_board"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "board_items",
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
    op.create_index(op.f("ix_board_items_tenant_id"), "board_items", ["tenant_id"])
    op.create_index(op.f("ix_board_items_owner_id"), "board_items", ["owner_id"])
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "board_items")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "board_items")
    op.drop_index(op.f("ix_board_items_owner_id"), table_name="board_items")
    op.drop_index(op.f("ix_board_items_tenant_id"), table_name="board_items")
    op.drop_table("board_items")
