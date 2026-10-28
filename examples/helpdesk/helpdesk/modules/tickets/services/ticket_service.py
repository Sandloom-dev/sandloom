"""Ticket business logic.

Shows the canonical cross-module patterns of the framework:

* **Sync contract** — organization membership is checked through the
  ``tenancy.membership.v1`` service-registry contract (resolved in the API
  layer and passed in), never by importing tenancy internals.
* **Typed events** — ``await TicketCreated(...).publish()`` writes a versioned
  event to the transactional outbox.
* **Audit** — ``record_audit`` records security-sensitive actions in the same
  Unit of Work.

The organization and author are referenced by plain UUID (no cross-module FK).
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit

from ..events import TicketCreated, TicketStatusChanged
from ..models import Ticket
from ..schemas import TicketCreateIn, TicketOut
from .operations import tickets_of_org


class TicketService:
    @staticmethod
    async def _ensure_member(
        membership: Any,
        organization_id: UUID,
        user_id: UUID,
        *,
        min_role: str | None = None,
    ) -> None:
        # Contract calls take no session: tenancy answers from its OWN
        # read-only Unit of Work (identical in a monolith and over RPC).
        if min_role is not None:
            ok = await membership.has_role_at_least(organization_id, user_id, min_role)
        else:
            ok = await membership.is_member(organization_id, user_id)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="you are not a member of this organization",
            )

    @classmethod
    async def create(
        cls,
        membership: Any,
        organization_id: UUID,
        data: TicketCreateIn,
        user_id: UUID,
        db: AsyncSession,
    ) -> Ticket:
        await cls._ensure_member(membership, organization_id, user_id)
        ticket = Ticket(
            organization_id=organization_id,
            created_by=user_id,
            subject=data.subject,
            body=data.body,
            priority=data.priority,
        )
        db.add(ticket)
        await db.flush()
        await db.refresh(ticket)
        await TicketCreated(
            ticket_id=ticket.id,
            organization_id=organization_id,
            actor_id=user_id,
            priority=ticket.priority,
        ).publish()
        record_audit(
            "tickets.created",
            actor_id=user_id,
            resource_type="ticket",
            resource_id=str(ticket.id),
            data={"organization_id": str(organization_id), "priority": ticket.priority},
        )
        return ticket

    @classmethod
    async def list_for_org(
        cls,
        membership: Any,
        organization_id: UUID,
        user_id: UUID,
        reads: Any,
        *,
        limit: int,
    ) -> list[TicketOut]:
        """The organization's ``limit`` newest tickets (members only).

        A declared read (``reads`` is the route's ``PlannedReads``): newest
        first, the id breaking ties of equal ``created_at``.
        """
        await cls._ensure_member(membership, organization_id, user_id)
        page = await reads.page(
            tickets_of_org, limit=limit, organization_id=organization_id
        )
        return list(page.items)

    @classmethod
    async def change_status(
        cls,
        membership: Any,
        organization_id: UUID,
        ticket_id: UUID,
        new_status: str,
        user_id: UUID,
        db: AsyncSession,
    ) -> Ticket:
        # Only admins/owners may move a ticket's status.
        await cls._ensure_member(membership, organization_id, user_id, min_role="admin")
        result = await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.organization_id == organization_id,
            )
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
            )
        old_status = ticket.status
        if new_status != old_status:
            ticket.status = new_status
            await db.flush()
            # Reload server-side onupdate columns (updated_at) so serialization
            # never triggers lazy IO on the async session.
            await db.refresh(ticket)
            await TicketStatusChanged(
                ticket_id=ticket.id,
                organization_id=organization_id,
                old_status=old_status,
                new_status=new_status,
                actor_id=user_id,
            ).publish()
            record_audit(
                "tickets.status_changed",
                actor_id=user_id,
                resource_type="ticket",
                resource_id=str(ticket.id),
                data={"old_status": old_status, "new_status": new_status},
            )
        return ticket
