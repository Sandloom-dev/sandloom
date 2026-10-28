"""billing_operation_intents: authorized class-F PSP operations (ADR-0003 G1)

Revision ID: 0004_billing
Revises: 0003_billing
Create Date: 2026-10-12 10:00:00

One row per checkout / customer-portal operation, committed BEFORE the PSP
call: the authorization it was granted on (actor, tenant, membership
version, parameters digest), the PSP idempotency key derived from it, and
its outcome (pending -> issued | revoked | failed). The webhook activates a
completed checkout only for an issued intent.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_billing"
down_revision: str | Sequence[str] | None = "0003_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "billing_operation_intents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("tenant_type", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=True),
        sa.Column("params_digest", sa.String(length=64), nullable=False),
        sa.Column("price_id", sa.Integer(), nullable=True),
        sa.Column("authorized_version", sa.BigInteger(), nullable=True),
        sa.Column("psp_idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("psp_session_id", sa.String(length=255), nullable=True),
        sa.Column("psp_session_url", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("followup_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        op.f("ix_billing_operation_intents_customer_id"),
        "billing_operation_intents",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_billing_operation_intents_psp_session_id"),
        "billing_operation_intents",
        ["psp_session_id"],
        unique=False,
    )
    op.create_index(
        "uq_billing_intent_live_request",
        "billing_operation_intents",
        ["actor_id", "operation", "request_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'issued')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_billing_intent_live_request", table_name="billing_operation_intents"
    )
    op.drop_index(
        op.f("ix_billing_operation_intents_psp_session_id"),
        table_name="billing_operation_intents",
    )
    op.drop_index(
        op.f("ix_billing_operation_intents_customer_id"),
        table_name="billing_operation_intents",
    )
    op.drop_table("billing_operation_intents")
