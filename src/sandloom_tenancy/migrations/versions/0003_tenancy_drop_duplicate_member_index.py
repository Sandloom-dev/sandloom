"""tenancy_members: drop the index duplicated by uq_tenancy_member

Revision ID: 0003_tenancy
Revises: 0002_tenancy
Create Date: 2026-10-09 12:00:00

``ix_tenancy_members_organization_id (organization_id)`` is a strict prefix of
the unique ``uq_tenancy_member (organization_id, user_id)`` index, which
already serves every ``WHERE organization_id = …`` lookup (role gate, member
list, contract calls). The duplicate only cost an extra index entry (and WAL)
on every membership write.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_tenancy"
down_revision: str | Sequence[str] | None = "0002_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_tenancy_members_organization_id"), table_name="tenancy_members"
    )


def downgrade() -> None:
    op.create_index(
        op.f("ix_tenancy_members_organization_id"),
        "tenancy_members",
        ["organization_id"],
        unique=False,
    )
