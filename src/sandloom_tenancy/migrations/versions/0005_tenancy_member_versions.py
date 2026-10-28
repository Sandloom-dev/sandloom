"""per-membership event versions

Revision ID: 0005_tenancy
Revises: 0004_tenancy
Create Date: 2026-10-11 10:00:00

``tenancy_member_versions (organization_id, user_id) -> version``: the
monotonic version every member lifecycle event carries. A separate table (not
a ``tenancy_members`` column) so the sequence survives a removal and continues
on a re-add. No backfill: the first change of an existing membership starts at
1, which a version-guarded consumer treats as newer than the version-less
events published before this revision (a NULL stored version is the oldest).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_tenancy"
down_revision: str | Sequence[str] | None = "0004_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenancy_member_versions",
        sa.Column("organization_id", postgresql.UUID(), nullable=False),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["tenancy_organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("tenancy_member_versions")
