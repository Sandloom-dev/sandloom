"""The later revision that validates the constraint the earlier one added NOT VALID."""

from alembic import op

revision = "0001"
down_revision = None


# --8<-- [start:revision]
def upgrade() -> None:
    op.execute("ALTER TABLE notes VALIDATE CONSTRAINT ck_notes_title_not_empty")


# --8<-- [end:revision]


def downgrade() -> None:
    pass
