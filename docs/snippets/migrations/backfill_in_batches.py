"""A revision that backfills a column in short, idempotent batches."""

from alembic import op

revision = "0001"
down_revision = None


# --8<-- [start:revision]
_BATCH = 5000


def upgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        while True:
            updated = bind.exec_driver_sql(
                "UPDATE notes SET archived = false WHERE id IN ("
                "SELECT id FROM notes WHERE archived IS NULL LIMIT %s)",
                (_BATCH,),
            ).rowcount
            if not updated:
                break


# --8<-- [end:revision]


def downgrade() -> None:
    pass
