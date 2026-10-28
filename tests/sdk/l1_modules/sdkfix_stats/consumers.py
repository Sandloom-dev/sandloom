"""A consumer of the notes module's channel through a view (Appendix B)."""

from __future__ import annotations

from uuid import UUID

from sandloom.sdk import Event, on

COUNTS: dict[UUID, int] = {}


class NoteCreated(Event, channel="sdkfix_notes.note.created.v1"):  # a view
    tenant_id: UUID


@on(NoteCreated)
async def count_note(event: NoteCreated) -> None:
    COUNTS[event.tenant_id] = COUNTS.get(event.tenant_id, 0) + 1
