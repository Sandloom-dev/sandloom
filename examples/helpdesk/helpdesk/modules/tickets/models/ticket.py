"""Ticket ORM model.

Tables are prefixed ``tickets_``. A ticket belongs to ONE organization, and
the organization is its tenant: ``TenantScoped`` adds the indexed
``tenant_id`` (the organization id), filled on insert and filtered on every
read. The author is referenced by plain UUID — no cross-module foreign keys
into tenancy/identity (that would block extracting this module into its own
service).
"""

import uuid

from sqlalchemy import Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time

TICKET_STATUSES = ("open", "pending", "closed")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")


class Ticket(TenantScoped, Base):
    """A support ticket of one organization (``tenant_id`` is its id)."""

    __tablename__ = "tickets_tickets"
    # The ticket list's keyset order (ListOperation ``tickets_of_org``):
    # WHERE tenant_id = … ORDER BY created_at DESC, id DESC — a backward
    # range scan instead of sorting every ticket of the organization.
    __table_args__ = (
        Index("ix_tickets_tickets_org_created_id", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
