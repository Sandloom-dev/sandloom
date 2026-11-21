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
autocommit block (writes keep flowing) with the runner's lock_timeout lifted
for the build, which waits for older snapshots; a re-run drops an INVALID index
of that name and builds it again, and is a no-op on a valid one.
Measurements: docs/design/sql-profile-2026-10-21.md.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from alembic import op

revision: str = "0006_identity"
down_revision: str | Sequence[str] | None = "0005_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_users_search_trgm"


@contextmanager
def _no_lock_timeout() -> Iterator[None]:
    """A concurrent build waits for every older snapshot of the database, and the
    runner's ``lock_timeout`` would cut that wait (leaving an INVALID index).
    The wait blocks no traffic: lift the timeout for the build, put it back."""
    bind = op.get_bind()
    previous = bind.exec_driver_sql("SHOW lock_timeout").scalar()
    bind.exec_driver_sql("SET lock_timeout = 0")
    try:
        yield
    finally:
        bind.exec_driver_sql(f"SET lock_timeout = '{previous}'")


def _drop_if_invalid(name: str) -> None:
    """``IF NOT EXISTS`` skips an INVALID index a failed build left: drop it."""
    invalid = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(%s) AND NOT indisvalid",
        (name,),
    )
    if invalid.first() is not None:
        op.execute(f"DROP INDEX CONCURRENTLY {name}")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block(), _no_lock_timeout():
        _drop_if_invalid(_INDEX)
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON users USING gin (full_name gin_trgm_ops, "
            "(email::text) gin_trgm_ops, username gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block(), _no_lock_timeout():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
