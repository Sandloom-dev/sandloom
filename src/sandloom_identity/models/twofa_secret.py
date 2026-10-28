"""Two-factor authentication secret model."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id

if TYPE_CHECKING:
    from .user import User


class TwoFASecret(Base):
    """Two-factor authentication secrets."""

    __tablename__ = "twofa_secrets"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    secret: Mapped[str] = mapped_column(String(255), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Last accepted TOTP time-step (unix_time // 30). A code is accepted only
    # for a strictly greater step, so an observed code cannot be replayed.
    last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[CreatedAt]
    enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="twofa_secret")

    # user_id lookups use the UNIQUE constraint's index (twofa_secrets_user_id_key).
