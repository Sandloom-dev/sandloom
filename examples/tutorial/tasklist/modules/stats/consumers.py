"""Consumers of the stats module."""

from uuid import UUID

from sandloom.sdk import Event, on

from . import services


# --8<-- [start:consumer]
class ItemCreated(Event, channel="tasks.item.created.v1"):
    """A view: fields of the producer (tasks/events.py)."""

    item_id: UUID
    tenant_id: UUID


@on(ItemCreated)
async def on_item_created(event: ItemCreated) -> None:
    """Runs once per event (durable), with the event's tenant bound."""
    await services.record_item()


# --8<-- [end:consumer]
