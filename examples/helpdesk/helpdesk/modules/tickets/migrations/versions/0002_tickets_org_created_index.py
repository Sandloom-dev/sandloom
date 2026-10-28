"""tickets: (organization_id, created_at) index for the org ticket list

Revision ID: 0002_tickets
Revises: 0001_tickets
Create Date: 2026-10-21 16:45:00

``TicketService.list_for_org`` selects ``WHERE organization_id = ? ORDER BY
created_at DESC LIMIT n``. With only ``(organization_id)`` it fetched every
ticket of the organization and top-N sorted them: on 500k tickets, an org
with 100k tickets took 45.9 ms; the composite index serves it as a backward
index scan in 0.09 ms (an org with 400 tickets: 0.73 → 0.32 ms).
Measurements: docs/design/sql-profile-2026-10-21.md (Sandloom repository).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_tickets"
down_revision: str | Sequence[str] | None = "0001_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_tickets_tickets_org_created",
        "tickets_tickets",
        ["organization_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_tickets_org_created", table_name="tickets_tickets")
