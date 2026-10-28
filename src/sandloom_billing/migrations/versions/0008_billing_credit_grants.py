"""Credit grants (refunds / disputes), negative credit balances (ADR-0003 item 12)

Revision ID: 0008_billing
Revises: 0007_billing
Create Date: 2026-10-12 20:00:00

- ``billing_credit_grants``: one row per credit-pack purchase, linking the
  PSP payment (``payment_intent``) to the credits granted, plus convergent
  reversed credits (open item 12). Purchases from before this revision have
  no row (their refunds / disputes are not reversed automatically).
- ``billing_psp_payment_states``: the latest refund / dispute state of a PSP
  payment, recorded even before its grant exists (out-of-order delivery).
- ``billing_credit_balances``: the ``amount >= 0`` CHECK is dropped — a
  reversal of already-spent credits leaves a negative (owed) balance.
  Downgrade restores the CHECK and therefore REFUSES while a negative
  balance exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_billing"
down_revision: str | Sequence[str] | None = "0007_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_billing_credit_balance_non_negative"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "billing_credit_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("psp_session_id", sa.String(length=255), nullable=False),
        sa.Column("psp_payment_intent_id", sa.String(length=255), nullable=True),
        sa.Column("price_id", sa.Integer(), nullable=False),
        sa.Column("credits", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reversed", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["billing_customers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("psp_session_id"),
    )
    op.create_index(
        op.f("ix_billing_credit_grants_customer_id"),
        "billing_credit_grants",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        "uq_billing_credit_grants_payment_intent",
        "billing_credit_grants",
        ["psp_payment_intent_id"],
        unique=True,
        postgresql_where=sa.text("psp_payment_intent_id IS NOT NULL"),
    )
    op.create_table(
        "billing_psp_payment_states",
        sa.Column("psp_payment_intent_id", sa.String(length=255), nullable=False),
        sa.Column("psp_customer_id", sa.String(length=255), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column(
            "amount_refunded", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("dispute_status", sa.String(length=8), nullable=True),
        sa.Column(
            "dispute_amount", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("psp_payment_intent_id"),
    )
    op.drop_constraint(_CHECK, "billing_credit_balances", type_="check")


def downgrade() -> None:
    """Downgrade schema (refuses while a balance is negative)."""
    op.create_check_constraint(_CHECK, "billing_credit_balances", "amount >= 0")
    op.drop_table("billing_psp_payment_states")
    op.drop_index(
        "uq_billing_credit_grants_payment_intent", table_name="billing_credit_grants"
    )
    op.drop_index(
        op.f("ix_billing_credit_grants_customer_id"),
        table_name="billing_credit_grants",
    )
    op.drop_table("billing_credit_grants")
