"""Events the fixture module produces (its own channels)."""

from __future__ import annotations

from uuid import UUID

from sandloom.sdk import Event


class NoteCreated(Event, channel="sdkfix_notes.note.created.v1"):
    note_id: UUID
    tenant_id: UUID
    title: str


class NoteSaved(Event, channel="sdkfix_notes.note.saved.v1"):
    note_id: UUID
    tenant_id: UUID
    title: str
    version: int


class NoteDeleted(Event, channel="sdkfix_notes.note.deleted.v1"):
    note_id: UUID
    tenant_id: UUID
    version: int


class NoteLiked(Event, channel="sdkfix_notes.note.liked.v1"):
    note_id: UUID
    tenant_id: UUID
    version: int
