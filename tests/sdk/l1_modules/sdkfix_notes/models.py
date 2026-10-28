"""Models of the fixture module (a private declarative base: never part of
the framework's metadata or migrations)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.sdk import TenantScoped


class FixtureBase(DeclarativeBase):
    pass


class Note(FixtureBase, TenantScoped):
    __tablename__ = "sdkfix_notes_note"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    title: Mapped[str] = mapped_column(String(200))


class NoteIndex(FixtureBase, TenantScoped):
    __tablename__ = "sdkfix_notes_note_index"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    note_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    like_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
