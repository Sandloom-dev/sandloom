"""The purge watermark of a projection (spec §6.4 item 5).

One row per projection that ever purged tombstones or rebuilt: the
``purged_before`` watermark (monotonic: only ever raised; the apply path
dead-letters an event emitted before it), the start of the oldest running
rebuild and how many run (a purge refuses while any does). A core
infrastructure table like the outbox: it lives in the projection's write
store and carries no tenant (a watermark is per projection, across tenants).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base


class ProjectionWatermark(Base):
    __tablename__ = "projection_watermarks"

    projection: Mapped[str] = mapped_column(String(200), primary_key=True)
    purged_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rebuild_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: how many rebuilds run (``rebuild_started_at`` is set while any does)
    rebuilds_active: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
