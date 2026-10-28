"""Consumers of the board module."""

from uuid import UUID

from sandloom.sdk import Event, on


class ItemCreated(Event, channel="notes.item.created.v1"):
    """A view: fields of the producer (notes/events.py)."""

    item_id: UUID
    tenant_id: UUID


@on(ItemCreated)
async def on_item_created(event: ItemCreated) -> None:
    """Runs once per event (durable), with the event's tenant bound."""


class InvoicePaid(Event, channel="billing.invoice.paid.v1"):  # unverified: no producer schema was found
    """A view: fields from --field."""

    invoice_id: UUID
    tenant_id: UUID
    amount: int


@on(InvoicePaid)
async def on_invoice_paid(event: InvoicePaid) -> None:
    """Runs once per event (durable), with the event's tenant bound."""
