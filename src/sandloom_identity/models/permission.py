"""Permission model."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id

if TYPE_CHECKING:
    from .role_permission import RolePermission


class Permission(Base):
    """Permission model for RBAC."""

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    resource: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    action: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[CreatedAt]

    # Relationships
    role_permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="permission", cascade="all, delete-orphan"
    )
