"""Ticket ORM model.

Tables are prefixed ``tickets_``. The owning organization and author are
referenced by plain UUID — no cross-module foreign keys into tenancy/identity
(that would block extracting this module into its own service).
"""

import uuid

from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time

TICKET_STATUSES = ("open", "pending", "closed")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")


class Ticket(Base):
    """A support ticket scoped to one organization."""

    __tablename__ = "tickets_tickets"
    # The ticket list's keyset order (ListOperation ``tickets_of_org``):
    # WHERE organization_id = … ORDER BY created_at DESC, id DESC — a backward
    # range scan instead of sorting every ticket of the organization.
    __table_args__ = (
        Index(
            "ix_tickets_tickets_org_created_id", "organization_id", "created_at", "id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
