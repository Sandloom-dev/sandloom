"""Events of the tickets module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


class TicketCreated(Event, channel="tickets.created.v1"):
    """A ticket was opened in an organization."""

    ticket_id: UUID
    organization_id: UUID
    actor_id: UUID
    priority: str


class TicketStatusChanged(Event, channel="tickets.status_changed.v1"):
    """A ticket moved from ``old_status`` to ``new_status``."""

    ticket_id: UUID
    organization_id: UUID
    old_status: str
    new_status: str
    actor_id: UUID
