"""Events of the invites module (its own channels)."""

from datetime import datetime
from uuid import UUID

from sandloom.sdk import Event


class InvitationSent(Event, channel="invites.invitation.sent.v1"):
    """An invitation link to deliver: the mail service sends ``token`` to
    ``email``; the activity feed records the invitation."""

    invitation_id: UUID
    tenant_id: UUID
    email: str
    token: str
    expires_at: datetime
    invited_by: UUID
