"""tickets are tenant scoped: the organization is the tenant

Revision ID: 0005_tickets
Revises: 0004_tickets

``Ticket`` inherits ``TenantScoped``: its ``organization_id`` column becomes the
indexed ``tenant_id`` the mixin declares (a rename, so every existing ticket
keeps its organization), the keyset index follows the column, and the table
gets the fail-closed row policy a strict production profile requires of every
``TenantScoped`` table.
"""

from collections.abc import Sequence

from alembic import op

from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS

revision: str = "0005_tickets"
down_revision: str | Sequence[str] | None = "0004_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The rename keeps the composite keyset index (its column list follows);
    # the single-column index is re-created under the mixin's name.
    op.drop_index(op.f("ix_tickets_tickets_organization_id"), table_name="tickets_tickets")
    op.alter_column("tickets_tickets", "organization_id", new_column_name="tenant_id")
    op.create_index(
        op.f("ix_tickets_tickets_tenant_id"), "tickets_tickets", ["tenant_id"]
    )
    enable_tenant_rls(op, "tickets_tickets")


def downgrade() -> None:
    disable_tenant_rls(op, "tickets_tickets")
    op.drop_index(op.f("ix_tickets_tickets_tenant_id"), table_name="tickets_tickets")
    op.alter_column("tickets_tickets", "tenant_id", new_column_name="organization_id")
    op.create_index(
        op.f("ix_tickets_tickets_organization_id"),
        "tickets_tickets",
        ["organization_id"],
    )
