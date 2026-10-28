"""saga_instances + saga_steps: saga instance state (ADR-0008 §2)

The orchestrator's version-guarded instance rows and the per-step table that
``sandloom sagas show`` and the metrics read. Not a dedupe record: step effects are
deduplicated by the provider's ``contract_command_log``. Additive: two new
tables. Under ``tenant_rls`` both get the tenant policy with system access, so
the timer sweep and the operator CLI reach every tenant only through the
system channel (``sandloom_system``; created before this migration in an RLS
deployment — without it the policy is installed without system access and the
sweep cannot see other tenants' instances).

Revision ID: 0010_saga_state
Revises: 0009_http_idempotency_log
Create Date: 2026-10-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from sandloom.core.orm import disable_tenant_rls, enable_tenant_rls

# revision identifiers, used by Alembic.
revision: str = "0010_saga_state"
down_revision: str | Sequence[str] | None = "0009_http_idempotency_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("saga_instances", "saga_steps")


def _rls_enabled() -> bool:
    return bool(op.get_context().config.attributes.get("sandloom_tenant_rls", False))


def _system_role_exists() -> bool:
    row = op.get_bind().execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'sandloom_system'")
    ).first()
    return row is not None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "saga_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("saga_type", sa.String(length=200), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("saga_id", sa.String(length=200), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("resolving", sa.Boolean(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("last_outcome", sa.String(length=20), nullable=True),
        sa.Column("step_first_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("step_deadline_at", sa.DateTime(), nullable=True),
        sa.Column("compensation_deadline_at", sa.DateTime(), nullable=True),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("applied", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.JSON(), nullable=True),
        sa.Column("park_reason", sa.String(length=64), nullable=True),
        sa.Column("parked_from", sa.String(length=20), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("started_by", sa.String(length=200), nullable=True),
        sa.Column("swept_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("saga_type", "saga_id", name="uq_saga_instances_type_id"),
    )
    op.create_index(
        "ix_saga_instances_due",
        "saga_instances",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('running', 'compensating')"),
    )
    op.create_index(
        "ix_saga_instances_status_updated",
        "saga_instances",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_saga_instances_tenant_id", "saga_instances", ["tenant_id"], unique=False
    )
    op.create_table(
        "saga_steps",
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=4), nullable=False),
        sa.Column("saga_type", sa.String(length=200), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(length=400), nullable=True),
        sa.Column("first_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_duration_ms", sa.Float(), nullable=True),
        sa.Column("last_error", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("instance_id", "step", "direction"),
    )
    op.create_index(
        "ix_saga_steps_tenant_id", "saga_steps", ["tenant_id"], unique=False
    )
    if _rls_enabled():
        system_access = _system_role_exists()
        for table in _TABLES:
            enable_tenant_rls(op, table, system_access=system_access)


def downgrade() -> None:
    """Downgrade schema."""
    if _rls_enabled():
        for table in reversed(_TABLES):
            disable_tenant_rls(op, table)
    op.drop_index("ix_saga_steps_tenant_id", table_name="saga_steps")
    op.drop_table("saga_steps")
    op.drop_index("ix_saga_instances_tenant_id", table_name="saga_instances")
    op.drop_index("ix_saga_instances_status_updated", table_name="saga_instances")
    op.drop_index("ix_saga_instances_due", table_name="saga_instances")
    op.drop_table("saga_instances")
