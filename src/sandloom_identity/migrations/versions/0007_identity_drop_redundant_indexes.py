"""identity: drop three indexes their UNIQUE indexes already cover

Revision ID: 0007_identity
Revises: 0006_identity
Create Date: 2026-10-21 16:30:00

- ``ix_twofa_secrets_user_id (user_id)`` duplicates the UNIQUE
  ``twofa_secrets_user_id_key (user_id)``;
- ``ix_user_roles_user_id (user_id)`` is the leading column of
  ``uq_user_roles_user_role (user_id, role_id)``;
- ``ix_role_permissions_role_id (role_id)`` is the leading column of
  ``uq_role_permissions_role_permission (role_id, permission_id)``.

Checked with EXPLAIN on 100k users / 100k role assignments before dropping:
the claims query, the admin-role check, the role-permissions load, the 2FA
secret lookup and ``DELETE FROM user_roles WHERE user_id = …`` (plus the FK
checks) all use the unique indexes. Each dropped index was one extra write per
insert (20k user_roles inserts: 385-437 ms → 349-371 ms). Dropped and recreated
CONCURRENTLY (autocommit block): no write lock on the tables.
Measurements: docs/design/sql-profile-2026-10-21.md.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_identity"
down_revision: str | Sequence[str] | None = "0006_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("ix_twofa_secrets_user_id", "twofa_secrets", "user_id"),
    ("ix_user_roles_user_id", "user_roles", "user_id"),
    ("ix_role_permissions_role_id", "role_permissions", "role_id"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _column in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, column in _INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column})"
            )
