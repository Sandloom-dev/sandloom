"""Project entity — owned by the projects module (its authoritative state)."""

import uuid

from sqlalchemy import BigInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time


class Project(TenantScoped, Base):
    """A project. ``version`` increments on every state change and is carried in
    the domain events so downstream projections can apply updates monotonically
    (last-writer-wins) under at-least-once, out-of-order delivery."""

    __tablename__ = "projects_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
