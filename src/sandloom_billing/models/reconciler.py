"""The intent reconciler's run record (its operational criterion).

One row per periodic job, written at the end of every completed run with the
database's clock. Billing's ``billing_reconciler`` health check reads it: a
run older than the allowed interval or a missing run degrades operational
health and prevents new checkout and portal effects. The reconciler is system
work across tenants and the row holds no tenant data.
"""

from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt
from sandloom.sdk.data import UtcDateTime


class BillingReconcilerRun(Base):
    """When a billing periodic job last completed a run."""

    __tablename__ = "billing_reconciler_runs"

    #: The job (``reconcile_operation_intents``).
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    #: End of the last completed run (database clock).
    finished_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    #: Completed runs since the row was created.
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[CreatedAt]
