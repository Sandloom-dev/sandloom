"""Invitations of an organization."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id

PENDING = "pending"
ACCEPTED = "accepted"
REVOKED = "revoked"


class Invitation(TenantScoped, Base):
    """One invitation; ``token_hash`` is the SHA-256 of the link token."""

    __tablename__ = "invites_invitations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    status: Mapped[str] = mapped_column(String(16), default=PENDING)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    invited_by: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[CreatedAt]
