"""Events of the notes module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


class ItemCreated(Event, channel="notes.item.created.v1"):
    item_id: UUID
    tenant_id: UUID


class NoteArchived(Event, channel="notes.note.archived.v1"):
    note_id: UUID
    tenant_id: UUID
