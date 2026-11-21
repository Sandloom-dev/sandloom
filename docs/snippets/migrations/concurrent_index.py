"""A revision that builds an index without blocking writes."""

from collections.abc import Iterator
from contextlib import contextmanager

from alembic import op

revision = "0001"
down_revision = None


# --8<-- [start:revision]
@contextmanager
def no_lock_timeout() -> Iterator[None]:
    bind = op.get_bind()
    previous = bind.exec_driver_sql("SHOW lock_timeout").scalar()
    bind.exec_driver_sql("SET lock_timeout = 0")
    try:
        yield
    finally:
        bind.exec_driver_sql(f"SET lock_timeout = '{previous}'")


def drop_if_invalid(name: str) -> None:
    invalid = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(%s) AND NOT indisvalid",
        (name,),
    )
    if invalid.first() is not None:
        op.execute(f"DROP INDEX CONCURRENTLY {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block(), no_lock_timeout():
        drop_if_invalid("ix_notes_owner_id")
        op.create_index(
            "ix_notes_owner_id",
            "notes",
            ["owner_id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block(), no_lock_timeout():
        op.drop_index(
            "ix_notes_owner_id",
            table_name="notes",
            postgresql_concurrently=True,
            if_exists=True,
        )


# --8<-- [end:revision]
