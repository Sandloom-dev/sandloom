"""billing: timestamps default to clock_timestamp()

Revision ID: 0011_billing
Revises: 0010_billing
Create Date: 2026-10-27 08:23:15.085523

``now()`` is the start of the transaction, so the rows one transaction
inserts tied on their timestamp; ``clock_timestamp()`` is the time of the
statement (the models' ``CreatedAt`` / ``UpdatedAt``). Only the column
default changes: a catalog-only ALTER, no table rewrite, and existing rows
keep their values.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_billing"
down_revision: str | Sequence[str] | None = "0010_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("billing_credit_grants", "created_at"),
    ("billing_credit_grants", "updated_at"),
    ("billing_credit_ledger", "created_at"),
    ("billing_operation_intents", "created_at"),
    ("billing_operation_intents", "updated_at"),
    ("billing_psp_payment_states", "updated_at"),
    ("billing_stripe_events", "received_at"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))
