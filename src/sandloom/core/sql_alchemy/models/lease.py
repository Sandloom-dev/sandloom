"""``infra_lease``: the table of the ``database`` lease driver (spec 5).

One row per lease name, kept for good: a released or expired name keeps its
row, so its ``fence`` survives and the next grant is higher. The row is written
only by the driver (``sandloom.core.drivers.database_lease``) in single
statements that compare ``expires_at`` with the database's own clock.

Like the other core infrastructure tables it carries no tenant policy: a lease
is not tenant data. The table is logged (an ``UNLOGGED`` table is emptied by a
crash, and the fence would restart).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


class InfraLease(Base):
    __tablename__ = "infra_lease"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: A diagnostic label of the holder (``<host>:<pid>``).
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Opaque, unique per grant: the proof of holding it.
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The database's clock: the lease is held while this is in the future.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Grows by one with every grant of the name.
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
