"""Enable fail-closed row policies for projects."""

from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision = "0002_projects"
down_revision = "0001_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    enable_tenant_rls(op, "projects_projects")


def downgrade() -> None:
    disable_tenant_rls(op, "projects_projects")
