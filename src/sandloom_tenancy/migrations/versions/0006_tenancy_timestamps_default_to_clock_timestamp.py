"""tenancy: timestamps default to clock_timestamp()

Revision ID: 0006_tenancy
Revises: 0005_tenancy
Create Date: 2026-10-27 08:23:11.771875

``now()`` is the start of the transaction, so the rows one transaction
inserts tied on their timestamp; ``clock_timestamp()`` is the time of the
statement (the models' ``CreatedAt`` / ``UpdatedAt``). Only the column
default changes: a catalog-only ALTER, no table rewrite, and existing rows
keep their values.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_tenancy"
down_revision: str | Sequence[str] | None = "0005_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("tenancy_invitations", "created_at"),
    ("tenancy_members", "created_at"),
    ("tenancy_organizations", "created_at"),
    ("tenancy_organizations", "updated_at"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))
