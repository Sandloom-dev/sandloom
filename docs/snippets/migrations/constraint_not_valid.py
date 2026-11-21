"""A revision that adds a check constraint without scanning the table."""

from alembic import op

revision = "0001"
down_revision = None


# --8<-- [start:revision]
def upgrade() -> None:
    op.execute(
        "ALTER TABLE notes ADD CONSTRAINT ck_notes_title_not_empty "
        "CHECK (title <> '') NOT VALID"
    )


# --8<-- [end:revision]


def downgrade() -> None:
    op.execute("ALTER TABLE notes DROP CONSTRAINT ck_notes_title_not_empty")
