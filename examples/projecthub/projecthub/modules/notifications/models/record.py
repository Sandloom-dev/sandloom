"""In-app notification record — owned by the notifications module.

``TenantScoped`` adds the indexed ``tenant_id`` and opts the table into automatic
tenant filtering, so the read API can never leak one tenant's notifications to
another.
"""

import uuid

from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class NotificationRecord(TenantScoped, Base):
    __tablename__ = "notifications_inapp"
    # the list's keyset order (ListOperation ``recent_notifications``):
    # WHERE tenant_id = … ORDER BY created_at DESC, id DESC
    __table_args__ = (
        Index("ix_notifications_inapp_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    recipient_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[CreatedAt]
