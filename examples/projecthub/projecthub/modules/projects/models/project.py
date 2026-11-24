"""Project entity — owned by the projects module (its authoritative state)."""

import uuid

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, UpdatedAt, new_id
from sandloom.sdk.data import (
    Versioned,
)  # sandloom: advanced version counted by the database


class Project(TenantScoped, Versioned, Base):
    """A project. ``version`` (``Versioned``) goes up by one on every state
    change and is carried in the domain events so downstream projections can
    apply updates monotonically (last-writer-wins) under at-least-once,
    out-of-order delivery."""

    __tablename__ = "projects_projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
