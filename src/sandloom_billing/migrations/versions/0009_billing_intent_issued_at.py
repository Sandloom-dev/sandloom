"""billing_operation_intents.issued_at: portal change attribution (ADR-0003 item 10)

Revision ID: 0009_billing
Revises: 0008_billing
Create Date: 2026-10-12 21:00:00

When an intent became ``issued`` (its URL was handed out). A portal intent's
``issued_at`` attributes its customer's later plan / cancellation changes;
a change with no portal intent issued in the attribution window is audited.
Existing issued intents stay NULL (they never attribute).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_billing"
down_revision: str | Sequence[str] | None = "0008_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "billing_operation_intents",
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("billing_operation_intents", "issued_at")
