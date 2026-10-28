"""notifications baseline: messages

Revision ID: 0001_notifications
Revises:
Create Date: 2026-10-25

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

from sandloom.sdk.data import enable_tenant_rls  # sandloom: advanced RLS

# revision identifiers, used by Alembic.
revision: str = "0001_notifications"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications_messages",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recipient_id", sa.UUID(), nullable=False),
        sa.Column("template", sa.String(length=100), nullable=False),
        sa.Column("operation_key", sa.String(length=400), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key"),
    )
    op.create_index(
        op.f("ix_notifications_messages_tenant_id"),
        "notifications_messages",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_notifications_messages_recipient_id"),
        "notifications_messages",
        ["recipient_id"],
    )
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "notifications_messages")


def downgrade() -> None:
    op.drop_index(
        op.f("ix_notifications_messages_recipient_id"),
        table_name="notifications_messages",
    )
    op.drop_index(
        op.f("ix_notifications_messages_tenant_id"), table_name="notifications_messages"
    )
    op.drop_table("notifications_messages")
