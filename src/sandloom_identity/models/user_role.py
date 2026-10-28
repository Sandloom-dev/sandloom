"""User-Role relationship model."""

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id

if TYPE_CHECKING:
    from .role import Role
    from .user import User


class UserRole(Base):
    """Many-to-many relationship between users and roles."""

    __tablename__ = "user_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )

    assigned_at: Mapped[CreatedAt]
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("users.id"), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User", foreign_keys=[user_id], back_populates="user_roles"
    )
    role: Mapped["Role"] = relationship("Role", back_populates="user_roles")
    assigned_by_user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[assigned_by]
    )

    __table_args__ = (
        # user_id lookups use uq_user_roles_user_role (its leading column).
        Index("ix_user_roles_role_id", "role_id"),
        Index("uq_user_roles_user_role", "user_id", "role_id", unique=True),
    )
