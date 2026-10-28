"""Events of the board module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


class ItemCreated(Event, channel="board.item.created.v1"):
    item_id: UUID
    tenant_id: UUID
