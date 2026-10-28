"""Enforce the TenantScoped model's policy in strict production deployments."""

from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision = "0003_notifications"
down_revision = "0002_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    enable_tenant_rls(op, "notifications_inapp")


def downgrade() -> None:
    disable_tenant_rls(op, "notifications_inapp")
