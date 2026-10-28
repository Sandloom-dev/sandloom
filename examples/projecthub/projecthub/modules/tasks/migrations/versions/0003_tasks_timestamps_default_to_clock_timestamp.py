"""tasks: timestamps default to clock_timestamp()

Revision ID: 0003_tasks
Revises: 0002_tasks
Create Date: 2026-10-27 08:27:40.004134

``now()`` is the start of the transaction, so the rows one transaction
inserts tied on their timestamp; ``clock_timestamp()`` is the time of the
statement (the models' ``CreatedAt`` / ``UpdatedAt``). Only the column
default changes: existing rows keep their values.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_tasks"
down_revision: str | Sequence[str] | None = "0002_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("tasks_tasks", "created_at"),
    ("tasks_tasks", "updated_at"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))
