"""Bench services: persistence + the transactional event (Sandloom conventions)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.di import publish_event
from benchsuite.common import EVENT_CHANNEL

from .models import BenchItem, BenchWrite


async def get_item(db: AsyncSession, item_id: int) -> BenchItem | None:
    return await db.get(BenchItem, item_id)


async def create_write(db: AsyncSession, name: str, value: int) -> int:
    """INSERT the row; the event lands in the outbox in the SAME transaction."""
    row = BenchWrite(name=name, value=value)
    db.add(row)
    await db.flush()
    new_id = row.id
    await publish_event(EVENT_CHANNEL, {"id": new_id, "name": name})
    return new_id
