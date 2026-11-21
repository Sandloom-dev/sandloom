"""A revision that waits for a table lock past the runner's lock_timeout."""

from alembic import op

revision = "0001"
down_revision = None


# --8<-- [start:revision]
def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = 0")
    op.execute("ALTER TABLE notes ADD COLUMN archived_at timestamptz")


# --8<-- [end:revision]


def downgrade() -> None:
    op.drop_column("notes", "archived_at")
