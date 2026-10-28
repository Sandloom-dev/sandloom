"""payments baseline: accounts and charges

Revision ID: 0001_payments
Revises:
Create Date: 2026-10-25

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

from sandloom.sdk.data import enable_tenant_rls  # sandloom: advanced RLS

# revision identifiers, used by Alembic.
revision: str = "0001_payments"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments_accounts",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "customer_id", name="uq_payments_accounts_customer"
        ),
    )
    op.create_index(
        op.f("ix_payments_accounts_tenant_id"), "payments_accounts", ["tenant_id"]
    )
    op.create_table(
        "payments_charges",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(length=400), nullable=False),
        sa.Column("refunded", sa.Boolean(), nullable=False),
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
        op.f("ix_payments_charges_tenant_id"), "payments_charges", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_payments_charges_customer_id"), "payments_charges", ["customer_id"]
    )
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "payments_accounts")
        enable_tenant_rls(op, "payments_charges")


def downgrade() -> None:
    op.drop_index(op.f("ix_payments_charges_customer_id"), table_name="payments_charges")
    op.drop_index(op.f("ix_payments_charges_tenant_id"), table_name="payments_charges")
    op.drop_table("payments_charges")
    op.drop_index(op.f("ix_payments_accounts_tenant_id"), table_name="payments_accounts")
    op.drop_table("payments_accounts")
