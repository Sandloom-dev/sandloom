"""Role model."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt

if TYPE_CHECKING:
    from .permission import Permission
    from .role_permission import RolePermission
    from .user_role import UserRole


class Role(Base):
    """Role model for RBAC."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    # Relationships
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole",
        back_populates="role",
        cascade="all, delete-orphan",
        foreign_keys="UserRole.role_id",
    )
    role_permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan"
    )

    @property
    def permissions(self) -> list["Permission"]:
        """Get permissions associated with this role."""
        return [rp.permission for rp in self.role_permissions if rp.permission]
