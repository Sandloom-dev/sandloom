"""invitations: hash tokens at rest + one open invitation per org/email

Revision ID: 0002_tenancy
Revises: 0001_tenancy
Create Date: 2026-10-08 00:00:00

* ``token`` (plaintext bearer secret) is replaced by ``token_hash`` =
  hex(sha256(utf8(token))). Existing tokens are backfilled in SQL, so
  invitations already sent keep working (lookup hashes the presented token).
* Expired-but-open invitations are superseded (``revoked_at`` set), duplicate
  open invitations for the same org+email keep only the newest, then a partial
  unique index enforces one open invitation per (organization_id, email).

Downgrade restores a ``token`` column but cannot recover plaintext: it is
filled with the hash, so open invitations become unusable after a downgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_tenancy"
down_revision: str | Sequence[str] | None = "0001_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenancy_invitations",
        sa.Column("token_hash", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE tenancy_invitations "
        "SET token_hash = encode(sha256(convert_to(token, 'UTF8')), 'hex')"
    )
    op.alter_column("tenancy_invitations", "token_hash", nullable=False)
    op.create_index(
        op.f("ix_tenancy_invitations_token_hash"),
        "tenancy_invitations",
        ["token_hash"],
        unique=True,
    )
    op.drop_index(op.f("ix_tenancy_invitations_token"), table_name="tenancy_invitations")
    op.drop_column("tenancy_invitations", "token")

    # Supersede expired open invitations (they can never be accepted).
    op.execute(
        "UPDATE tenancy_invitations SET revoked_at = now() "
        "WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at <= now()"
    )
    # Keep only the newest open invitation per org+email.
    op.execute(
        "UPDATE tenancy_invitations AS t SET revoked_at = now() "
        "WHERE t.accepted_at IS NULL AND t.revoked_at IS NULL AND EXISTS ("
        "  SELECT 1 FROM tenancy_invitations AS n"
        "  WHERE n.organization_id = t.organization_id AND n.email = t.email"
        "    AND n.accepted_at IS NULL AND n.revoked_at IS NULL"
        "    AND (n.created_at, n.id) > (t.created_at, t.id))"
    )
    op.create_index(
        "uq_tenancy_invitations_open_email",
        "tenancy_invitations",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_tenancy_invitations_open_email", table_name="tenancy_invitations"
    )
    op.add_column(
        "tenancy_invitations",
        sa.Column("token", sa.String(length=64), nullable=True),
    )
    # Plaintext is unrecoverable: store the hash (open invitations become dead).
    op.execute("UPDATE tenancy_invitations SET token = token_hash")
    op.alter_column("tenancy_invitations", "token", nullable=False)
    op.create_index(
        op.f("ix_tenancy_invitations_token"),
        "tenancy_invitations",
        ["token"],
        unique=True,
    )
    op.drop_index(
        op.f("ix_tenancy_invitations_token_hash"), table_name="tenancy_invitations"
    )
    op.drop_column("tenancy_invitations", "token_hash")
