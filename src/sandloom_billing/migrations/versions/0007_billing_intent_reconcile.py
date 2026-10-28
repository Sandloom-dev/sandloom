"""billing_operation_intents: reconciliation state and session expiry (#72)

Revision ID: 0007_billing
Revises: 0006_billing
Create Date: 2026-10-12 18:00:00

``billing_operation_intents``:

- ``psp_session_expires_at`` — when the PSP expires the checkout session
  (open item 13: a retry past it answers 410 instead of a dead URL);
- ``psp_expired_at`` — when billing confirmed the session can no longer be
  paid (NULL on a revoked intent with a session = compensation still owed);
- ``reconcile_attempts`` / ``reconcile_error`` — bounded retries of the
  periodic reconciler (#72), then ``followup_at``.

Existing rows: every new column is NULL / 0. A revoked intent from before
this revision has ``reconcile_attempts = 0`` and is never picked up by the
compensation retry (its expire outcome was not recorded; the session is
expired by the PSP within 24 h anyway).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_billing"
down_revision: str | Sequence[str] | None = "0006_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTENTS = "billing_operation_intents"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        _INTENTS,
        sa.Column("psp_session_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _INTENTS, sa.Column("psp_expired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        _INTENTS,
        sa.Column(
            "reconcile_attempts", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(_INTENTS, sa.Column("reconcile_error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column(_INTENTS, "reconcile_error")
    op.drop_column(_INTENTS, "reconcile_attempts")
    op.drop_column(_INTENTS, "psp_expired_at")
    op.drop_column(_INTENTS, "psp_session_expires_at")
