"""``infra_atomic_store``: the table of the ``database`` atomic_store driver (spec 4).

One row per key: ``key``, ``value`` and ``expires_at`` (``NULL``: the key never
expires, as the enqueue-dedupe pending record). Every statement of the driver
(``sandloom.core.drivers.database_atomic_store``) treats a row with
``expires_at <= now()`` as absent, against the database's own clock; the relay's
sweeper (``purge_expired_atomic_entries``) deletes the rows later.

Like the other core infrastructure tables it carries no tenant policy: a
revocation mark or a dedupe reservation is not tenant data. The table is
**logged** (an ``UNLOGGED`` table is emptied by a crash, which would lose
revocation marks): the driver refuses an unlogged one at ``aopen``. It is
rewritten by every ``set`` and every revocation, so it is created with a
``fillfactor`` that leaves room for in-place (HOT) updates (``expires_at`` is
deliberately not indexed, so an update stays HOT) and with an autovacuum that
runs on a small share of dead rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base

#: PostgreSQL storage parameters of the table (the migration applies the same).
STORAGE_PARAMETERS = {
    "fillfactor": 70,
    "autovacuum_vacuum_scale_factor": 0.02,
    "autovacuum_vacuum_threshold": 50,
    "autovacuum_analyze_scale_factor": 0.05,
}


class InfraAtomicStore(Base):
    __tablename__ = "infra_atomic_store"
    __table_args__ = ({"postgresql_with": STORAGE_PARAMETERS},)

    #: At most ``MAX_KEY_BYTES`` (1024) bytes, checked by the driver.
    key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    #: The database's clock: the key is live while this is in the future or NULL.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
