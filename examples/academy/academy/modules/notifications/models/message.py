"""A message recorded for a recipient."""

import uuid

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class NotificationsMessage(TenantScoped, Base):
    __tablename__ = "notifications_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    recipient_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    template: Mapped[str] = mapped_column(String(100), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    created_at: Mapped[CreatedAt]
