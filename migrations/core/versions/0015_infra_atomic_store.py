"""infra_atomic_store: the table of the ``database`` atomic_store driver

One row per key (spec 4, ``database_atomic_store``): the value and the
database-clock expiry (``NULL``: never expires). Logged on purpose (an unlogged
table is emptied by a crash and a revocation mark would be lost). No tenant
policy: it is not tenant data. Created with a ``fillfactor`` that leaves room
for HOT updates and an autovacuum that runs on a small share of dead rows
(PostgreSQL storage parameters; SQLite ignores them). Additive: one new table.

Revision ID: 0015_infra_atomic_store
Revises: 0014_infra_lease
Create Date: 2026-11-14 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_infra_atomic_store"
down_revision: str | Sequence[str] | None = "0014_infra_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "infra_atomic_store",
        sa.Column("key", sa.String(length=1024), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key"),
        postgresql_with={
            "fillfactor": 70,
            "autovacuum_vacuum_scale_factor": 0.02,
            "autovacuum_vacuum_threshold": 50,
            "autovacuum_analyze_scale_factor": 0.05,
        },
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("infra_atomic_store")
