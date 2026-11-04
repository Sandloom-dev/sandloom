"""Organization invitation model.

Invitation tokens are bearer secrets: only ``sha256(token)`` is stored
(``token_hash``); the raw token is returned once to the inviter and e-mailed to
the invitee, never persisted or logged.

Tenant scoping: see the note on ``OrganizationMember`` (membership.py)
— the same reasoning applies; every query filters on ``organization_id``
explicitly (or on the unique ``token_hash`` for accept).
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import CaseInsensitiveText, partial_index

INVITATION_TTL_DAYS = 14


def new_invitation_token() -> str:
    """A fresh, URL-safe, high-entropy invitation token (raw, never stored)."""
    return secrets.token_urlsafe(32)


def hash_invitation_token(token: str) -> str:
    """sha256 hex digest of a raw token — the only form that is persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS)


class OrganizationInvitation(Base):
    """Email invitation into an organization."""

    __tablename__ = "tenancy_invitations"
    __table_args__ = (
        # At most ONE open (not accepted, not revoked) invitation per org+email.
        # ``email`` is CaseInsensitiveText, so the uniqueness ignores case. Expiry is
        # not in the predicate (now() is not immutable): creating a new
        # invitation first supersedes (revokes) the expired open ones.
        partial_index(
            "uq_tenancy_invitations_open_email",
            "organization_id",
            "email",
            unique=True,
            where="accepted_at IS NULL AND revoked_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenancy_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(CaseInsensitiveText, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=default_expiry
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[CreatedAt]

    def is_pending(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return self.accepted_at is None and self.revoked_at is None and expires > now
