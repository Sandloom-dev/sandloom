"""taskboard tables are TenantScoped: tenant index + fail-closed RLS

Revision ID: 0004_taskboard
Revises: 0003_taskboard

The projection and its project snapshot inherit ``TenantScoped`` (the ORM
tenant guard), so they get the indexed ``tenant_id`` the mixin declares and
the row policy a strict production profile requires of every TenantScoped
table. Writes go through the monotonic helpers with the consumer's tenant
bound; under ``tenant_rls`` an unbound cross-tenant enumeration
(``rebuild_all``, a cold ``rebuild_entity``) sees no rows — rebuild with
``--tenant`` there.
"""

from collections.abc import Sequence

from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision: str = "0004_taskboard"
down_revision: str | Sequence[str] | None = "0003_taskboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_index(
        op.f("ix_taskboard_project_task_query_index_tenant_id"),
        "taskboard_project_task_query_index",
        ["tenant_id"],
    )
    enable_tenant_rls(op, "taskboard_project_task_query_index")
    op.create_index(
        op.f("ix_taskboard_project_snapshot_tenant_id"),
        "taskboard_project_snapshot",
        ["tenant_id"],
    )
    enable_tenant_rls(op, "taskboard_project_snapshot")


def downgrade() -> None:
    disable_tenant_rls(op, "taskboard_project_snapshot")
    op.drop_index(
        op.f("ix_taskboard_project_snapshot_tenant_id"),
        table_name="taskboard_project_snapshot",
    )
    disable_tenant_rls(op, "taskboard_project_task_query_index")
    op.drop_index(
        op.f("ix_taskboard_project_task_query_index_tenant_id"),
        table_name="taskboard_project_task_query_index",
    )
