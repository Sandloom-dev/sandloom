"""contract_command_log.effects: what a command left on its caller

A command's durable dedupe record keeps, beside its result, the call effects
its execution left on the caller's request (risks, remote calls in doubt,
the subject's self-revocation, pending step-ups; JSON). A replay re-applies
them, so it marks the caller exactly like the first execution (G-SENS-04,
G-UNIT-02). Additive: a new nullable column.

Revision ID: 0013_contract_command_effects
Revises: 0012_projection_rebuilds_active
Create Date: 2026-10-26 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_contract_command_effects"
down_revision: str | Sequence[str] | None = "0012_projection_rebuilds_active"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "contract_command_log",
        sa.Column("effects", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("contract_command_log", "effects")
