"""Role-Permission relationship model."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id

if TYPE_CHECKING:
    from .permission import Permission
    from .role import Role


class RolePermission(Base):
    """Many-to-many relationship between roles and permissions."""

    __tablename__ = "role_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )

    assigned_at: Mapped[CreatedAt]

    # Relationships
    role: Mapped["Role"] = relationship("Role", back_populates="role_permissions")
    permission: Mapped["Permission"] = relationship(
        "Permission", back_populates="role_permissions"
    )

    __table_args__ = (
        # role_id lookups use uq_role_permissions_role_permission (leading).
        Index("ix_role_permissions_permission_id", "permission_id"),
        Index(
            "uq_role_permissions_role_permission",
            "role_id",
            "permission_id",
            unique=True,
        ),
    )
