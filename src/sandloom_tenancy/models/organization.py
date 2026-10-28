"""Organization model (tenant root)."""

import uuid

from sqlalchemy import Boolean, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt


class Organization(Base):
    """A billable / collaborative tenant.

    NOTE: owner_id intentionally has no FK to users — modules stay
    schema-independent so they can be split into services later.
    """

    __tablename__ = "tenancy_organizations"
    # the organization lists' keyset order (ListOperation active_orgs_of_user)
    __table_args__ = (Index("ix_tenancy_organizations_created", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
