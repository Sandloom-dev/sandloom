"""http_idempotency_log: durable HTTP Idempotency-Key records

The record of ``idempotency_guard(durable=True)`` — ``(route scope, caller,
tenant, Idempotency-Key) -> (request fingerprint, status, stored response)`` —
claimed in the REQUEST's own Unit of Work and completed right before its
``COMMIT``, so it commits iff the endpoint's effect commits (ADR-0005; see
``sandloom.core.sql_alchemy.request_log``). Additive: a new table, nothing else
changes. Expired rows are purged by the relay housekeeping.

Revision ID: 0009_http_idempotency_log
Revises: 0008_outbox_trace_id
Create Date: 2026-10-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_http_idempotency_log"
down_revision: str | Sequence[str] | None = "0008_outbox_trace_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "http_idempotency_log",
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("args_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_http_idempotency_log_expires_at",
        "http_idempotency_log",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_http_idempotency_log_expires_at", table_name="http_idempotency_log"
    )
    op.drop_table("http_idempotency_log")
