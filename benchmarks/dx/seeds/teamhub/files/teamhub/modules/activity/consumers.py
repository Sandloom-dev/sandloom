"""Consumers of the activity module."""

from datetime import datetime
from uuid import UUID

from sandloom.sdk import Event, on

from . import services


class InvitationSent(Event, channel="invites.invitation.sent.v1"):
    """A view: fields of the producer (invites/events.py)."""

    invitation_id: UUID
    tenant_id: UUID
    email: str
    token: str
    expires_at: datetime
    invited_by: UUID


@on(InvitationSent)
async def on_invitation_sent(event: InvitationSent) -> None:
    """Runs once per event (durable), with the event's tenant bound."""
    await services.record(
        kind="invitation_sent",
        summary=f"Invitation sent to {event.email}",
        actor_id=event.invited_by,
        subject_id=event.invitation_id,
    )
