"""identity security hardening

* ``refresh_tokens.family_id`` — rotation family for refresh-token reuse
  detection (a replayed, already-rotated token revokes the whole family).
  Existing rows are backfilled with their own id (each becomes its own family).
* ``twofa_secrets.last_used_step`` — last accepted TOTP time-step, so a code
  cannot be replayed within its validity window.
* ``users.password_reset_nonce`` — binds the single outstanding password-reset
  link (single-use; a newer link invalidates older ones).

Revision ID: 0002_identity
Revises: 0001_identity
Create Date: 2026-10-08 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_identity"
down_revision: str | Sequence[str] | None = "0001_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("refresh_tokens", sa.Column("family_id", sa.UUID(), nullable=True))
    op.execute("UPDATE refresh_tokens SET family_id = id WHERE family_id IS NULL")
    op.alter_column("refresh_tokens", "family_id", nullable=False)
    op.create_index(
        "ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"], unique=False
    )

    op.add_column(
        "twofa_secrets", sa.Column("last_used_step", sa.BigInteger(), nullable=True)
    )

    op.add_column(
        "users",
        sa.Column("password_reset_nonce", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "password_reset_nonce")
    op.drop_column("twofa_secrets", "last_used_step")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
