"""Ticket business logic.

Shows the canonical cross-module patterns of the framework:

* **Sync contract** — organization membership is the route's gate
  (``OrgRole`` of the generated ``tenancy.membership.v1`` stub): the service
  runs only for a member, and never imports tenancy internals.
* **Tenant = organization** — a ticket is ``TenantScoped``; the route has
  checked the caller's membership of the path's organization, so the service
  binds that organization as the tenant (``bind_tenant``) and every insert and
  query is scoped to it.
* **Typed events** — ``await TicketCreated(...).publish()`` writes a versioned
  event to the transactional outbox.
* **Audit** — ``record_audit`` records security-sensitive actions in the same
  Unit of Work.

``db()`` is the request's session. The author is referenced by plain UUID (no
cross-module FK).
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from sandloom.sdk import db, record_audit
from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced organization = tenant

from ..events import TicketCreated, TicketStatusChanged
from ..models import Ticket
from ..schemas import TicketCreateIn, TicketOut
from .operations import tickets_of_org


class TicketService:
    @classmethod
    async def create(
        cls, organization_id: UUID, data: TicketCreateIn, user_id: UUID
    ) -> Ticket:
        with bind_tenant(organization_id):
            ticket = Ticket(
                created_by=user_id,
                subject=data.subject,
                body=data.body,
                priority=data.priority,
            )
            db().add(ticket)
            await db().flush()
            await db().refresh(ticket)
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
        cls, organization_id: UUID, reads: Any, *, limit: int
    ) -> list[TicketOut]:
        """The organization's ``limit`` newest tickets.

        A declared read (``reads`` is the route's ``PlannedReads``): newest
        first, the id breaking ties of equal ``created_at``.
        """
        with bind_tenant(organization_id):
            page = await reads.page(tickets_of_org, limit=limit)
        return list(page.items)

    @classmethod
    async def change_status(
        cls, organization_id: UUID, ticket_id: UUID, new_status: str, user_id: UUID
    ) -> Ticket:
        with bind_tenant(organization_id):
            result = await db().execute(select(Ticket).where(Ticket.id == ticket_id))
            ticket = result.scalar_one_or_none()
            if ticket is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="ticket not found"
                )
            old_status = ticket.status
            if new_status != old_status:
                ticket.status = new_status
                await db().flush()
                # Reload server-side onupdate columns (updated_at) so
                # serialization never triggers lazy IO on the async session.
                await db().refresh(ticket)
        if new_status != old_status:
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
