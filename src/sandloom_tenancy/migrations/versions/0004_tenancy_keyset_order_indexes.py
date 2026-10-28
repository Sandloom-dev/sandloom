"""keyset-order indexes for the member and organization lists

Revision ID: 0004_tenancy
Revises: 0003_tenancy
Create Date: 2026-10-10 23:00:00

The member list (``GET /orgs/{id}/members``) and the organization list
(``GET /orgs``, ``list_user_organization_ids``) are declared
``ListOperation``s ordered by ``(created_at, id)``. A declared order must be
backed by an index (the planner refuses to plan a keyset page that would sort
the table):

* ``ix_tenancy_members_org_created (organization_id, created_at, id)`` serves
  ``WHERE organization_id = … ORDER BY created_at, id`` and the keyset
  ``(created_at, id) > (…)`` without a sort;
* ``ix_tenancy_organizations_created (created_at, id)``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_tenancy"
down_revision: str | Sequence[str] | None = "0003_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_tenancy_members_org_created",
        "tenancy_members",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_tenancy_organizations_created",
        "tenancy_organizations",
        ["created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tenancy_organizations_created", table_name="tenancy_organizations")
    op.drop_index("ix_tenancy_members_org_created", table_name="tenancy_members")
