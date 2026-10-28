"""Events of the tasks module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


class ItemCreated(Event, channel="tasks.item.created.v1"):
    item_id: UUID
    tenant_id: UUID
