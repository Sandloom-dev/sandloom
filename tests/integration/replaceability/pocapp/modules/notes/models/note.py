"""A tenant row (table created with RLS by the test, or the PoC's setup_db.py)."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped, new_id


class NotesItem(TenantScoped, Base):
    __tablename__ = "notes_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    body: Mapped[str] = mapped_column(String(200))
