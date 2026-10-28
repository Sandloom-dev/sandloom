"""billing_operation_intents: partial indexes for the intent reconciler

Revision ID: 0010_billing
Revises: 0009_billing
Create Date: 2026-10-21 14:00:00

The reconciler (every ``RECONCILE_INTERVAL_SECONDS``) selects stale
``pending`` intents and revoked checkouts whose session expire failed, each
``ORDER BY created_at LIMIT n``. Neither had an index: both were parallel
seq scans of the whole intents table, which grows with every checkout and
portal session. On 200k intents: 35 ms → 0.16 ms and 19 ms → 0.7 ms. The
matching rows are rare and transient, so both indexes stay at a few pages;
``status`` was already in the live-request unique index's predicate, so the
pending → issued update was never HOT and pays no new cost.
Measurements: docs/design/sql-profile-2026-10-21.md.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_billing"
down_revision: str | Sequence[str] | None = "0009_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTENTS = "billing_operation_intents"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_billing_intents_pending_created",
        _INTENTS,
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_billing_intents_owed_expiry",
        _INTENTS,
        ["created_at"],
        unique=False,
        postgresql_where=sa.text(
            "status = 'revoked' AND operation = 'checkout' "
            "AND psp_expired_at IS NULL AND reconcile_attempts > 0"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_billing_intents_owed_expiry", table_name=_INTENTS)
    op.drop_index("ix_billing_intents_pending_created", table_name=_INTENTS)
