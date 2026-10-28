"""billing_credit_ledger: drop the index duplicated by the idempotency UNIQUE

Revision ID: 0003_billing
Revises: 0002_billing
Create Date: 2026-10-09 12:00:00

``ix_billing_credit_ledger_customer_id (customer_id)`` is a strict prefix of
``uq_billing_credit_ledger_customer_operation (customer_id, operation_key)``
(0002), which already serves every ``WHERE customer_id = …`` lookup and the
FK's ON DELETE CASCADE. The duplicate only cost an extra index entry (and WAL)
on every credit operation.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_billing"
down_revision: str | Sequence[str] | None = "0002_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_billing_credit_ledger_customer_id"),
        table_name="billing_credit_ledger",
    )


def downgrade() -> None:
    op.create_index(
        op.f("ix_billing_credit_ledger_customer_id"),
        "billing_credit_ledger",
        ["customer_id"],
        unique=False,
    )
