"""enrollment baseline: enrollments

Revision ID: 0001_enrollment
Revises:
Create Date: 2026-10-25

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

from sandloom.sdk.data import enable_tenant_rls  # sandloom: advanced RLS

# revision identifiers, used by Alembic.
revision: str = "0001_enrollment"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enrollment_enrollments",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("student_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_enrollment_enrollments_tenant_id"),
        "enrollment_enrollments",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_enrollment_enrollments_student_id"),
        "enrollment_enrollments",
        ["student_id"],
    )
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "enrollment_enrollments")


def downgrade() -> None:
    op.drop_index(
        op.f("ix_enrollment_enrollments_student_id"),
        table_name="enrollment_enrollments",
    )
    op.drop_index(
        op.f("ix_enrollment_enrollments_tenant_id"), table_name="enrollment_enrollments"
    )
    op.drop_table("enrollment_enrollments")
