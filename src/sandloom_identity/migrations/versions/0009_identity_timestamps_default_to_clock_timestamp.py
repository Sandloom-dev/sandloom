"""identity: timestamps default to clock_timestamp()

Revision ID: 0009_identity
Revises: 0008_identity
Create Date: 2026-10-27 08:23:07.835319

``now()`` is the start of the transaction, so the rows one transaction
inserts tied on their timestamp; ``clock_timestamp()`` is the time of the
statement (the models' ``CreatedAt`` / ``UpdatedAt``). Only the column
default changes: a catalog-only ALTER, no table rewrite, and existing rows
keep their values.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_identity"
down_revision: str | Sequence[str] | None = "0008_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("external_identities", "created_at"),
    ("external_identities", "updated_at"),
    ("permissions", "created_at"),
    ("refresh_tokens", "created_at"),
    ("role_permissions", "assigned_at"),
    ("roles", "created_at"),
    ("roles", "updated_at"),
    ("twofa_backup_codes", "created_at"),
    ("twofa_secrets", "created_at"),
    ("user_roles", "assigned_at"),
    ("users", "created_at"),
    ("users", "updated_at"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))
