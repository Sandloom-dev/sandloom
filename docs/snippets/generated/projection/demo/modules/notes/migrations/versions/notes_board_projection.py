"""notes.board projection table (by `sandloom add projection`)

Revision ID: notes_board_projection
Revises: 0001_notes
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision: str = "notes_board_projection"
down_revision: str | Sequence[str] | None = "0001_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notes_board_rows",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("board_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "board_id"),
    )
    op.create_index(op.f("ix_notes_board_rows_tenant_id"), "notes_board_rows", ["tenant_id"])
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "notes_board_rows")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "notes_board_rows")
    op.drop_index(op.f("ix_notes_board_rows_tenant_id"), table_name="notes_board_rows")
    op.drop_table("notes_board_rows")
