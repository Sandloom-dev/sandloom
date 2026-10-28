"""The activity use cases. ``db()`` is the session of the running unit."""

from uuid import UUID

import sqlalchemy as sa

from sandloom.sdk import db

from .models.entry import ActivityEntry
from .schemas import EntryOut


async def record(*, kind: str, summary: str, actor_id: UUID, subject_id: UUID) -> None:
    """One feed entry of the bound organization."""
    db().add(
        ActivityEntry(
            kind=kind, summary=summary, actor_id=actor_id, subject_id=subject_id
        )
    )
    await db().flush()


async def list_entries(*, limit: int) -> list[EntryOut]:
    # created_at (clock_timestamp()), then the UUIDv7 id: no tie, ever.
    newest_first = (ActivityEntry.created_at.desc(), ActivityEntry.id.desc())
    query = sa.select(ActivityEntry).order_by(*newest_first).limit(limit)
    rows = await db().scalars(query)
    return [EntryOut.model_validate(row) for row in rows]
