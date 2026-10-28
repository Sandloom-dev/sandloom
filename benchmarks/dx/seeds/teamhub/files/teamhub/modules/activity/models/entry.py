"""Entries of an organization's activity feed."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class ActivityEntry(TenantScoped, Base):
    __tablename__ = "activity_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(500))
    actor_id: Mapped[uuid.UUID] = mapped_column()
    subject_id: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[CreatedAt]
