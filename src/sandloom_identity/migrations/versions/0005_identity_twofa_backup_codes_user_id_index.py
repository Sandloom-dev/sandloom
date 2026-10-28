"""twofa_backup_codes: index user_id

Revision ID: 0005_identity
Revises: 0004_identity
Create Date: 2026-10-21 13:41:07

Every backup-code statement filters by ``user_id`` (list unused codes,
consume one, regenerate = delete + insert), and deleting a user runs the
``twofa_backup_codes_user_id_fkey`` check on it. With no index each of them
scanned the whole table: on 100k codes 5.2-5.8 ms per statement (warm) →
0.02-0.05 ms, and the FK check on a user DELETE 5.0 → 0.1 ms. Codes are
written ten at a time on 2FA setup only, so the index costs nothing on a hot
path. Measurements: docs/design/sql-profile-2026-10-21.md.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_identity"
down_revision: str | Sequence[str] | None = "0004_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_twofa_backup_codes_user_id",
        "twofa_backup_codes",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_twofa_backup_codes_user_id", table_name="twofa_backup_codes")
