"""Task entity — owned by the tasks module. ``project_id`` references a project
by plain UUID (no cross-module FK, per the Sandloom isolation rule)."""

import uuid

from sqlalchemy import BigInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time


class Task(TenantScoped, Base):
    __tablename__ = "tasks_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
