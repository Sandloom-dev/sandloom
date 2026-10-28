"""Two-factor authentication backup codes model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt

if TYPE_CHECKING:
    from .user import User


class TwoFABackupCode(Base):
    """Two-factor authentication backup code model."""

    __tablename__ = "twofa_backup_codes"
    # Every read / consume / regenerate filters by user, and deleting a user
    # checks this FK: without the index each one scans the whole table.
    __table_args__ = (Index("ix_twofa_backup_codes_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[CreatedAt]

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="twofa_backup_codes")
