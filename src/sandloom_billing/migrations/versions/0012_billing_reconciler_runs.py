"""billing_reconciler_runs: the intent reconciler's run record

Revision ID: 0012_billing
Revises: 0011_billing
Create Date: 2026-11-08 23:30:00

One row per periodic job, written at the end of every completed run of
``reconcile_operation_intents`` with the database's clock. Billing's
``billing_reconciler`` health check reads it: no run within the allowed
interval means no worker or no beat runs the reconciler, and readiness of
the processes that load billing says so (KB-IC-3). A one-row table: the
write is one upsert per run (every 300 s), the read one primary-key lookup
per health TTL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_billing"
down_revision: str | Sequence[str] | None = "0011_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "billing_reconciler_runs",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("billing_reconciler_runs")
