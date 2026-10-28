"""keyset-order index for the ticket list (replaces the 0002 org/created index)

Revision ID: 0003_tickets
Revises: 0002_tickets
Create Date: 2026-10-21 19:00:00

``GET /orgs/{id}/tickets`` is a declared ``ListOperation`` ordered by
``(created_at, id)`` descending. The planner refuses a list page that would
sort the matching rows: ``ix_tickets_tickets_org_created_id
(organization_id, created_at, id)`` serves ``WHERE organization_id = …
ORDER BY created_at DESC, id DESC LIMIT n`` as a backward index range scan.
It also serves every query the 0002 ``(organization_id, created_at)`` index
did, so that one is dropped.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_tickets"
down_revision: str | Sequence[str] | None = "0002_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_tickets_tickets_org_created_id",
        "tickets_tickets",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.drop_index("ix_tickets_tickets_org_created", table_name="tickets_tickets")


def downgrade() -> None:
    op.create_index(
        "ix_tickets_tickets_org_created",
        "tickets_tickets",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.drop_index("ix_tickets_tickets_org_created_id", table_name="tickets_tickets")
