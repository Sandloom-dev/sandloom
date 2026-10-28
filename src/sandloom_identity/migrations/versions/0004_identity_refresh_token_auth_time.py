"""refresh_tokens: add auth_time (instant of the primary authentication)

Revision ID: 0004_identity
Revises: 0003_identity
Create Date: 2026-10-21 12:30:00

Step-up re-authentication of password-less accounts trusted the access
token's ``iat``, which every refresh renews — a stolen refresh token alone
passed it. ``auth_time`` records when the rotation family's login happened;
refresh carries it unchanged into the ``auth_time`` access-token claim.
Existing rows stay NULL (no claim: such sessions sign in again to step up).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_identity"
down_revision: str | Sequence[str] | None = "0003_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("auth_time", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "auth_time")
