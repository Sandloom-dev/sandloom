"""identity: users.password_reset_required (operator-forced password reset)

Revision ID: 0008_identity
Revises: 0007_identity
Create Date: 2026-10-22 12:00:00

Set by ``sandloom security force-password-reset`` (the ``identity.security.v1``
contract) after a credential / database leak: a password sign-in of such a
user is refused with 403 until the password-reset flow (or a password
change) clears it. ``server_default false``: a metadata-only ALTER on
PostgreSQL 11+, no table rewrite.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_identity"
down_revision: str | Sequence[str] | None = "0007_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "password_reset_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "password_reset_required")
