"""Task entity — owned by the tasks module. ``project_id`` references a project
by plain UUID (no cross-module FK, per the Sandloom isolation rule)."""

import uuid

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, UpdatedAt, new_id
from sandloom.sdk.data import (
    Versioned,
)  # sandloom: advanced version counted by the database


class Task(TenantScoped, Versioned, Base):
    __tablename__ = "tasks_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
