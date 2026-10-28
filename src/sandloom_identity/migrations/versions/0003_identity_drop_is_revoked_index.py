"""refresh_tokens: drop the unselective boolean index on is_revoked

Revision ID: 0003_identity
Revises: 0002_identity
Create Date: 2026-10-09 13:40:00

``ix_refresh_tokens_is_revoked (is_revoked)`` indexes a two-valued column. No
query uses it: every ``is_revoked`` predicate is paired with an indexed,
selective column (``id`` primary key, the UNIQUE ``token``, ``family_id`` or
``user_id``), and the revoked-token cleanup filters on ``revoked_at``. It only
cost an extra index write (and WAL) on every token mint and revocation.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_identity"
down_revision: str | Sequence[str] | None = "0002_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_refresh_tokens_is_revoked", table_name="refresh_tokens")


def downgrade() -> None:
    op.create_index(
        "ix_refresh_tokens_is_revoked", "refresh_tokens", ["is_revoked"], unique=False
    )
