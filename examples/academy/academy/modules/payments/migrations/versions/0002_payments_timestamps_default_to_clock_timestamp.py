"""payments: timestamps default to clock_timestamp()

Revision ID: 0002_payments
Revises: 0001_payments
Create Date: 2026-10-27 08:27:18.901469

``now()`` is the start of the transaction, so the rows one transaction
inserts tied on their timestamp; ``clock_timestamp()`` is the time of the
statement (the models' ``CreatedAt``). Only the column default changes:
existing rows keep their values.

Autogenerate also found the baseline's plain ``tenant_id`` index on
``payments_accounts``, which the model no longer declares: the
``(tenant_id, customer_id)`` unique key serves every lookup it would.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_payments"
down_revision: str | Sequence[str] | None = "0001_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (("payments_charges", "created_at"),)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("clock_timestamp()"))
    op.drop_index(op.f("ix_payments_accounts_tenant_id"), table_name="payments_accounts")


def downgrade() -> None:
    op.create_index(
        op.f("ix_payments_accounts_tenant_id"), "payments_accounts", ["tenant_id"]
    )
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))
