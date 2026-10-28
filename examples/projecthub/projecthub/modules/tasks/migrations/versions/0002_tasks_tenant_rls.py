"""Enable fail-closed row policies for tasks."""

from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision = "0002_tasks"
down_revision = "0001_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    enable_tenant_rls(op, "tasks_tasks")


def downgrade() -> None:
    disable_tenant_rls(op, "tasks_tasks")
