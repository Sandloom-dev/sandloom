"""keyset-order index for the in-app notification list

Revision ID: 0002_notifications
Revises: 0001_notifications
Create Date: 2026-10-21 12:00:00

``GET /api/v1/notifications`` is a declared, tenant-scoped ``ListOperation``
ordered by ``(created_at, id)`` descending. The planner refuses a list page
that would sort the matching rows: ``ix_notifications_inapp_tenant_created
(tenant_id, created_at, id)`` serves ``WHERE tenant_id = … ORDER BY
created_at DESC, id DESC LIMIT n`` as a backward index range scan.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_notifications"
down_revision: str | Sequence[str] | None = "0001_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_notifications_inapp_tenant_created",
        "notifications_inapp",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notifications_inapp_tenant_created", table_name="notifications_inapp"
    )
