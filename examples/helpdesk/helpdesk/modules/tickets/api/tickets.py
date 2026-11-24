"""Routes of the tickets module: ``/api/v1/orgs/{organization_id}/tickets``.

``module_router()`` authenticates every route (a ``CurrentUser``). A ticket is
``TenantScoped`` and its tenant is the organization in the PATH, so the router
binds no tenant from the token (``tenant="none"``): the service binds the path's
organization once the route's gate has checked the caller's membership. Every
route is gated by ``OrgRole`` of ``tenancy.membership.v1`` (live; 403 for a
non-member or a role below the minimum; 501 where tenancy is not deployed). The
premium ``export`` route is gated by the ORGANIZATION's subscription:
``require(Subscribed(of=Account.org(param="organization_id")))`` (live through
billing; 402 without an active one). The caller's personal subscription does not
open an organization's export. Persistence stays out of this layer; the service
holds the logic. A retry of ``create`` with the same ``Idempotency-Key`` replays
the stored body (``replay_safe``: the answer echoes the caller's own fields and
the new ticket id).
"""

from typing import Any
from uuid import UUID

from fastapi import Depends, Query, status

from helpdesk.shared.contracts.billing_entitlements_v1 import Subscribed
from helpdesk.shared.contracts.tenancy_membership_v1 import OrgRole
from sandloom.sdk import Account, CurrentUser, module_router, require
from sandloom.sdk.data import (  # sandloom: advanced read-only planner routes
    PlannedReads,
    UowReads,
)
from sandloom.sdk.web import (  # sandloom: advanced opt in to body replay
    replay_safe,
)

from ..schemas import TicketCreateIn, TicketOut, TicketStatusIn
from ..services import TicketService
from ..services.operations import MAX_TICKETS_PAGE

router = module_router(prefix="/api/v1/orgs", tags=["tickets"], tenant="none")

MEMBER = Depends(require(OrgRole("member", org_param="organization_id")))
ADMIN = Depends(require(OrgRole("admin", org_param="organization_id")))


@router.post(
    "/{organization_id}/tickets",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        MEMBER,
        replay_safe(reason="echoes the caller's own fields and the new ticket id"),
    ],
)
async def create_ticket(
    organization_id: UUID, data: TicketCreateIn, user: CurrentUser
) -> TicketOut:
    """Open a ticket in an organization the caller belongs to."""
    ticket = await TicketService.create(organization_id, data, user.user_id)
    return TicketOut.model_validate(ticket)


@router.get(
    "/{organization_id}/tickets",
    response_model=list[TicketOut],
    dependencies=[MEMBER],
)
async def list_tickets(
    organization_id: UUID,
    reads: PlannedReads,
    limit: int = Query(default=50, ge=1, le=MAX_TICKETS_PAGE),
) -> list[TicketOut]:
    """List recent tickets for an organization the caller belongs to.

    A read-only route: one declared keyset read (no Unit of Work).
    """
    return await TicketService.list_for_org(organization_id, reads, limit=limit)


@router.post(
    "/{organization_id}/tickets/{ticket_id}/status",
    response_model=TicketOut,
    dependencies=[ADMIN],
)
async def change_ticket_status(
    organization_id: UUID, ticket_id: UUID, data: TicketStatusIn, user: CurrentUser
) -> TicketOut:
    """Change a ticket's status (admins/owners only; audited + event)."""
    ticket = await TicketService.change_status(
        organization_id, ticket_id, data.status, user.user_id
    )
    return TicketOut.model_validate(ticket)


@router.get(
    "/{organization_id}/tickets/export",
    dependencies=[
        MEMBER,
        Depends(require(Subscribed(of=Account.org(param="organization_id")))),
    ],
)
async def export_tickets(organization_id: UUID, reads: UowReads) -> dict[str, Any]:
    """Premium export — requires the organization's active subscription."""
    tickets = await TicketService.list_for_org(
        organization_id, reads, limit=MAX_TICKETS_PAGE
    )
    return {
        "organization_id": str(organization_id),
        "count": len(tickets),
        "tickets": [t.model_dump(mode="json") for t in tickets],
    }
