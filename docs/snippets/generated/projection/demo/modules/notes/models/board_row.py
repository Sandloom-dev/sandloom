"""Rows of the notes.board projection (a read model)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped


class BoardRow(TenantScoped, Base):
    __tablename__ = "notes_board_rows"

    board_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
