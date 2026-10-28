"""users: pg_trgm GIN index for the admin user search

Revision ID: 0006_identity
Revises: 0005_identity
Create Date: 2026-10-21 16:00:00

``UserService.list_users(search=…)`` filters ``full_name ILIKE '%t%' OR
email::text ILIKE '%t%' OR username ILIKE '%t%'`` (count + page). Without an
index both statements scanned the whole table: 146 ms / 142 ms on 100k users.
With ``ix_users_search_trgm`` (one multi-column GIN, pg_trgm) they are
1.8 ms / 1.4 ms. Terms under 3 characters have no trigram and still scan.
Cost: GIN entries on every users UPDATE (``last_login_at`` keeps those
non-HOT): 20k login updates 690 → 1075 ms, ~19 µs each.

``CREATE EXTENSION pg_trgm`` needs CREATE on the database (pg_trgm is a
trusted extension) or a superuser pre-creating it, as
deploy/postgres/init-databases.sh does. The index is built CONCURRENTLY in an
autocommit block (writes keep flowing); IF NOT EXISTS makes a re-run a no-op.
Measurements: docs/design/sql-profile-2026-10-21.md.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_identity"
down_revision: str | Sequence[str] | None = "0005_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_search_trgm "
            "ON users USING gin (full_name gin_trgm_ops, "
            "(email::text) gin_trgm_ops, username gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_users_search_trgm")
