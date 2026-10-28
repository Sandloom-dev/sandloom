"""keyset-order index for the taskboard list

Revision ID: 0003_taskboard
Revises: 0002_taskboard
Create Date: 2026-10-22 09:00:00

``GET /api/v1/taskboard/tasks`` is a declared ``ListOperation`` filtered on the
caller's tenant and ordered by ``(updated_at, task_id)`` descending, with
OPTIONAL status / project-name-prefix filters. The planner refuses a list page
that would sort the matching rows: ``ix_taskboard_ptqi_tenant_updated
(tenant_id, updated_at, task_id)`` serves every filter combination as a
backward index range scan (the optional filters are residual predicates;
``ix_taskboard_ptqi_main`` still serves the canonical both-statuses query).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_taskboard"
down_revision: str | Sequence[str] | None = "0002_taskboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_taskboard_ptqi_tenant_updated",
        "taskboard_project_task_query_index",
        ["tenant_id", "updated_at", "task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_taskboard_ptqi_tenant_updated",
        table_name="taskboard_project_task_query_index",
    )
