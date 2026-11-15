"""``infra_atomic_store``: the table of the ``database`` atomic_store driver (spec 4).

One row per key: ``key``, ``value`` and ``expires_at`` (``NULL``: the key never
expires, as the enqueue-dedupe pending record). Every statement of the driver
(``sandloom.core.drivers.database_atomic_store``) treats a row with
``expires_at <= now()`` as absent, against the database's own clock; the driver
itself deletes the rows later (a periodic task of its own, on its own engine).

Like the other core infrastructure tables it carries no tenant policy: a
revocation mark or a dedupe reservation is not tenant data. The table is
**logged** (an ``UNLOGGED`` table is emptied by a crash, which would lose
revocation marks): the driver refuses an unlogged one at ``aopen``. The
PostgreSQL storage parameters (``fillfactor``, autovacuum thresholds; ``expires_at``
is deliberately not indexed, so an update stays HOT) live in migration 0015 only.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


class InfraAtomicStore(Base):
    __tablename__ = "infra_atomic_store"

    #: At most ``MAX_KEY_BYTES`` (1024) bytes, checked by the driver.
    key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    #: The database's clock: the key is live while this is in the future or NULL.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
