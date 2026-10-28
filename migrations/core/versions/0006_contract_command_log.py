"""contract_command_log: durable command-contract idempotency

The framework's command-contract dedupe record — ``(contract.method, bound
tenant, idempotency key) -> (args digest, status, encoded result)`` — moves from
the cache into the PROVIDER's database: the row is claimed and completed in the
command's own Unit of Work, so it commits atomically with the business change
(see ``sandloom.core.sql_alchemy.command_log``).

Revision ID: 0006_contract_command_log
Revises: 0005_outbox_partial_indexes
Create Date: 2026-10-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_contract_command_log"
down_revision: str | Sequence[str] | None = "0005_outbox_partial_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "contract_command_log",
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("contract", sa.String(length=300), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("args_digest", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_contract_command_log_expires_at",
        "contract_command_log",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_contract_command_log_expires_at", table_name="contract_command_log"
    )
    op.drop_table("contract_command_log")
