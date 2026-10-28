"""Refresh token model."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt

if TYPE_CHECKING:
    from .user import User


class RefreshToken(Base):
    """Refresh tokens for JWT authentication."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Rotation family: every token minted by refreshing descends from one login
    # and shares its family_id. Presenting an already-rotated (revoked) token is
    # a reuse signal and revokes the whole family.
    family_id: Mapped[uuid.UUID] = mapped_column(
        UUID, nullable=False, default=uuid.uuid4
    )

    # Instant of the PRIMARY authentication (password / SSO / 2FA login) that
    # started this rotation family, carried unchanged through every refresh
    # and embedded as the access token's ``auth_time`` claim — step-up checks
    # trust it, never the refreshed token's ``iat``. NULL on rows minted
    # before the column existed: their access tokens carry no ``auth_time``.
    auth_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    token: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[CreatedAt]

    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )
