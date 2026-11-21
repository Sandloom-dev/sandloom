"""A revision that builds an index without blocking writes."""

from alembic import op

revision = "0001"
down_revision = None


# --8<-- [start:revision]
def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_notes_owner_id",
            "notes",
            ["owner_id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_notes_owner_id",
            table_name="notes",
            postgresql_concurrently=True,
            if_exists=True,
        )


# --8<-- [end:revision]
