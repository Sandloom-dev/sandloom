"""The settings row of an organization (one per organization)."""

import uuid

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time


class WorkspaceSettings(TenantScoped, Base):
    __tablename__ = "workspace_settings"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    updated_by: Mapped[uuid.UUID] = mapped_column()
    updated_at: Mapped[UpdatedAt]
