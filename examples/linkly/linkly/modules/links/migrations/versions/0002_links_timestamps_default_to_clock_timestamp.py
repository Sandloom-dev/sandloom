"""links: timestamps default to clock_timestamp()

Revision ID: 0002_links
Revises: 0001_links
Create Date: 2026-10-27 08:27:06.736395

``now()`` is the start of the transaction, so the rows one transaction
inserts tied on their timestamp; ``clock_timestamp()`` is the time of the
statement (the models' ``CreatedAt`` / ``UpdatedAt``). Only the column
default changes: existing rows keep their values.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_links"
down_revision: str | Sequence[str] | None = "0001_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("links_shortlinks", "created_at"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, server_default=sa.text("now()"))
