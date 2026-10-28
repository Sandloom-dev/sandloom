"""Events of the links module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


class LinkCreated(Event, channel="links.created.v1"):
    """A short code was created."""

    link_id: UUID
    code: str
