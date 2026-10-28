"""billing_subscriptions.origin: intent-backed vs intent-less (ADR-0003 item 9)

Revision ID: 0005_billing
Revises: 0004_billing
Create Date: 2026-10-12 14:30:00

``origin`` records how a subscription row came to exist: ``intent`` (created
for an issued operation intent), ``psp`` (created by a webhook without an
intent marker: operator-created in the PSP, migrated, restored) or
``legacy`` (existed before this revision — backfilled here, so an upgrade
does not cut off paying customers). Syncing the PSP's state never depends on
it; granting entitlements to a ``psp`` row needs
BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true.

The column is added with ``'legacy'`` (the backfill of every existing row),
then its default becomes ``'psp'`` — fail closed: any later insert without an
origin (old code during a rolling deploy, a manual INSERT) is intent-less.

Downgrade drops the column, so the intent / psp distinction is LOST: a
downgrade -> upgrade cycle relabels every row ``legacy`` (granting again).
Do not downgrade past this revision on a database that has ``psp`` rows
without re-reviewing them (the downgrade refuses unless forced, see below).
"""

import os

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_billing"
down_revision: str | Sequence[str] | None = "0004_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "billing_subscriptions",
        sa.Column(
            "origin",
            sa.String(length=16),
            server_default="legacy",  # backfill: rows that exist now
            nullable=False,
        ),
    )
    op.alter_column(
        "billing_subscriptions",
        "origin",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        server_default="psp",  # every later insert: fail closed
    )


def downgrade() -> None:
    """Downgrade schema (refuses to erase intent-less markers unless forced)."""
    bind = op.get_bind()
    psp_rows = bind.execute(
        sa.text("SELECT count(*) FROM billing_subscriptions WHERE origin = 'psp'")
    ).scalar_one()
    if psp_rows and os.environ.get("SANDLOOM_BILLING_DROP_ORIGIN") != "1":
        raise RuntimeError(
            f"{psp_rows} intent-less (origin='psp') subscriptions would be "
            "relabelled 'legacy' (granting) by a later upgrade; review them, "
            "then set SANDLOOM_BILLING_DROP_ORIGIN=1 to downgrade anyway"
        )
    op.drop_column("billing_subscriptions", "origin")
