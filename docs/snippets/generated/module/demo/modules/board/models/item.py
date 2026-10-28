"""An example table: replace it with your domain."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class BoardItem(TenantScoped, Base):
    """``TenantScoped``: rows belong to the bound tenant and are filtered by it."""

    __tablename__ = "board_items"

    # new_id: a time-ordered UUIDv7; CreatedAt: clock_timestamp(), not now()
    # (the transaction's start), so rows of one transaction never tie.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True)
    created_at: Mapped[CreatedAt]
