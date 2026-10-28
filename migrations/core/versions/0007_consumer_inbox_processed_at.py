"""consumer_inbox: index processed_at for the retention purge

``DurableConsumerInbox.purge_processed_before`` (``sandloom outbox purge
--inbox-days``) deletes claims by ``processed_at < cutoff``. Without an index
every call scanned the whole table — on 500k claims 7.7k pages / ~135 ms even
when nothing was old enough — and the purge ran as one unbounded DELETE. The
purge is now batched (``id IN (SELECT … LIMIT n)``) and served by this index
(empty run 0.015 ms; a 1000-row batch 6.0 → 2.0 ms). Cost: one extra,
append-only index entry per durable claim (processed_at is monotonic).
Measurements: docs/design/sql-profile-2026-10-21.md.

Revision ID: 0007_consumer_inbox_processed_at
Revises: 0006_contract_command_log
Create Date: 2026-10-21 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_consumer_inbox_processed_at"
down_revision: str | Sequence[str] | None = "0006_contract_command_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_consumer_inbox_processed_at",
        "consumer_inbox",
        ["processed_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_consumer_inbox_processed_at", table_name="consumer_inbox")
