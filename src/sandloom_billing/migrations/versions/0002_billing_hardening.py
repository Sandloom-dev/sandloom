"""billing hardening: per-customer credit idempotency, BIGINT credits,
credit-pack amounts on prices, webhook ordering guard on subscriptions.

Revision ID: 0002_billing
Revises: 0001_billing
Create Date: 2026-10-08 00:00:00

- billing_credit_ledger: UNIQUE(operation_key) -> UNIQUE(customer_id,
  operation_key); delta -> BIGINT; + balance_after (original result replay).
- billing_credit_balances.amount -> BIGINT.
- billing_prices: + credits (credits granted by one purchase of the price).
- billing_subscriptions: + last_event_at / last_event_id (monotonic webhooks).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_billing"
down_revision: str | Sequence[str] | None = "0001_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001 declared the global UNIQUE(operation_key) without a name; PostgreSQL
# named it <table>_<column>_key.
_OLD_LEDGER_UNIQUE = "billing_credit_ledger_operation_key_key"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(_OLD_LEDGER_UNIQUE, "billing_credit_ledger", type_="unique")
    op.create_unique_constraint(
        "uq_billing_credit_ledger_customer_operation",
        "billing_credit_ledger",
        ["customer_id", "operation_key"],
    )
    op.alter_column(
        "billing_credit_ledger",
        "delta",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.add_column(
        "billing_credit_ledger",
        sa.Column("balance_after", sa.BigInteger(), nullable=True),
    )
    op.alter_column(
        "billing_credit_balances",
        "amount",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )

    op.add_column("billing_prices", sa.Column("credits", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_billing_price_credits_positive",
        "billing_prices",
        "credits IS NULL OR credits > 0",
    )

    op.add_column(
        "billing_subscriptions",
        sa.Column("last_event_at", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "billing_subscriptions",
        sa.Column("last_event_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema (fails if per-customer keys now collide globally)."""
    op.drop_column("billing_subscriptions", "last_event_id")
    op.drop_column("billing_subscriptions", "last_event_at")

    op.drop_constraint(
        "ck_billing_price_credits_positive", "billing_prices", type_="check"
    )
    op.drop_column("billing_prices", "credits")

    op.alter_column(
        "billing_credit_balances",
        "amount",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.drop_column("billing_credit_ledger", "balance_after")
    op.alter_column(
        "billing_credit_ledger",
        "delta",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.drop_constraint(
        "uq_billing_credit_ledger_customer_operation",
        "billing_credit_ledger",
        type_="unique",
    )
    op.create_unique_constraint(
        _OLD_LEDGER_UNIQUE, "billing_credit_ledger", ["operation_key"]
    )
