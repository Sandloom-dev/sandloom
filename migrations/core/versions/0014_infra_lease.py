"""infra_lease: the table of the ``database`` lease driver

One row per lease name (spec 5, ``database_lease``): owner, token, the
database-clock expiry and the fence, which survives release and expiry because
the row is kept. Logged on purpose (an unlogged table is emptied by a crash and
the fence would restart). No tenant policy: a lease is not tenant data.
Additive: one new table.

Revision ID: 0014_infra_lease
Revises: 0013_contract_command_effects
Create Date: 2026-11-14 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_infra_lease"
down_revision: str | Sequence[str] | None = "0013_contract_command_effects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "infra_lease",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("infra_lease")
