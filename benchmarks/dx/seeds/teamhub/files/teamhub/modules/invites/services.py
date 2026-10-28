"""The invites use cases. ``db()`` is the session of the running unit."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa

from sandloom.sdk import db

from .events import InvitationSent
from .models.invitation import PENDING, Invitation
from .schemas import InvitationIn, InvitationOut

#: How long an invitation link stays valid.
INVITATION_TTL = timedelta(days=7)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def send_invitation(body: InvitationIn, *, invited_by: UUID) -> InvitationOut:
    """Record a pending invitation and announce its link (the mail service
    delivers it)."""
    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        email=body.email.lower(),
        status=PENDING,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + INVITATION_TTL,
        invited_by=invited_by,
    )
    db().add(invitation)
    await db().flush()
    await db().refresh(invitation)
    await InvitationSent(
        invitation_id=invitation.id,
        tenant_id=invitation.tenant_id,
        email=invitation.email,
        token=token,
        expires_at=invitation.expires_at,
        invited_by=invited_by,
    ).publish()
    return InvitationOut.model_validate(invitation)


async def list_invitations(*, limit: int) -> list[InvitationOut]:
    """The organization's newest invitations first (``TenantScoped``)."""
    newest_first = (Invitation.created_at.desc(), Invitation.id.desc())
    query = sa.select(Invitation).order_by(*newest_first).limit(limit)
    rows = await db().scalars(query)
    return [InvitationOut.model_validate(row) for row in rows]
