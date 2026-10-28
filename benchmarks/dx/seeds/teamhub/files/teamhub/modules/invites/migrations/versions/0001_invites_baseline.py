"""invites baseline: the invitations table

Revision ID: 0001_invites
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

revision: str = "0001_invites"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "invites_invitations"


def upgrade() -> None:
    op.create_table(
        "invites_invitations",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_invites_invitations_tenant_id"), TABLE, ["tenant_id"])
    op.create_index(op.f("ix_invites_invitations_email"), TABLE, ["email"])
    op.create_index(op.f("ix_invites_invitations_expires_at"), TABLE, ["expires_at"])
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "invites_invitations")


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "invites_invitations")
    op.drop_index(op.f("ix_invites_invitations_expires_at"), table_name=TABLE)
    op.drop_index(op.f("ix_invites_invitations_email"), table_name=TABLE)
    op.drop_index(op.f("ix_invites_invitations_tenant_id"), table_name=TABLE)
    op.drop_table("invites_invitations")
