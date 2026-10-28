"""Routes of the tickets module: ``/api/v1/orgs/{organization_id}/tickets``.

``module_router()`` authenticates every route (a ``CurrentUser``); tickets
are not ``TenantScoped`` (see ``architecture_rules.yml``), so the router
binds no tenant. Every route is gated by ``tenancy.membership.v1``. The
premium ``export`` route is gated by the ORGANIZATION's subscription:
``require(Subscribed(of=Account.org(param="organization_id")))`` (live
through billing; 402 without an active one). The caller's personal
subscription does not open an organization's export. Persistence stays out
of this layer; the service holds the logic.
"""

from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status

from sandloom.sdk import CurrentUser, Subscribed, module_router, require
from sandloom.sdk.authz import (
    Account,  # sandloom: advanced explicit billing account (spec §7)
)
from sandloom.sdk.data import (  # sandloom: advanced read-only planner routes
    PlannedReads,
    UowReads,
)
from sandloom.sdk.infra import (
    get_core,  # sandloom: advanced L2 lookup of a stub-less contract
)
from sandloom.sdk.web import (  # sandloom: advanced db() lands with H1; explicit guard
    DbSession,
    idempotency_guard,
)

from ..schemas import TicketCreateIn, TicketOut, TicketStatusIn
from ..services import TicketService
from ..services.operations import MAX_TICKETS_PAGE

router = module_router(prefix="/api/v1/orgs", tags=["tickets"])

MEMBERSHIP_CONTRACT = "tenancy.membership.v1"


def _membership_service(core: Any = Depends(get_core)) -> Any:
    """Resolve the tenancy membership read-contract, or 501 if not deployed."""
    try:
        return core.get_service(MEMBERSHIP_CONTRACT)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="tenancy module is not deployed in this process",
        ) from exc


@router.post(
    "/{organization_id}/tickets",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_ticket(
    organization_id: UUID,
    data: TicketCreateIn,
    db: DbSession,
    user: CurrentUser,
    membership: Any = Depends(_membership_service),
    # After authentication: create_app refuses a guard solved before it.
    _key: str | None = Depends(idempotency_guard(scope="tickets", required=False)),
) -> TicketOut:
    """Open a ticket in an organization the caller belongs to."""
    ticket = await TicketService.create(
        membership, organization_id, data, user.user_id, db
    )
    return TicketOut.model_validate(ticket)


@router.get(
    "/{organization_id}/tickets",
    response_model=list[TicketOut],
)
async def list_tickets(
    organization_id: UUID,
    reads: PlannedReads,
    user: CurrentUser,
    membership: Any = Depends(_membership_service),
    limit: int = Query(default=50, ge=1, le=MAX_TICKETS_PAGE),
) -> list[TicketOut]:
    """List recent tickets for an organization the caller belongs to.

    A read-only route: one declared keyset read (no Unit of Work).
    """
    return await TicketService.list_for_org(
        membership, organization_id, user.user_id, reads, limit=limit
    )


@router.post(
    "/{organization_id}/tickets/{ticket_id}/status",
    response_model=TicketOut,
)
async def change_ticket_status(
    organization_id: UUID,
    ticket_id: UUID,
    data: TicketStatusIn,
    db: DbSession,
    user: CurrentUser,
    membership: Any = Depends(_membership_service),
) -> TicketOut:
    """Change a ticket's status (admins/owners only; audited + event)."""
    ticket = await TicketService.change_status(
        membership, organization_id, ticket_id, data.status, user.user_id, db
    )
    return TicketOut.model_validate(ticket)


@router.get(
    "/{organization_id}/tickets/export",
    dependencies=[
        Depends(require(Subscribed(of=Account.org(param="organization_id"))))
    ],
)
async def export_tickets(
    organization_id: UUID,
    # UowReads: the organization's live subscription gate shares the request
    # unit (a PlannedReads route takes no Unit of Work)
    reads: UowReads,
    user: CurrentUser,
    membership: Any = Depends(_membership_service),
) -> dict[str, Any]:
    """Premium export — requires the organization's active subscription."""
    tickets = await TicketService.list_for_org(
        membership, organization_id, user.user_id, reads, limit=MAX_TICKETS_PAGE
    )
    return {
        "organization_id": str(organization_id),
        "count": len(tickets),
        "tickets": [t.model_dump(mode="json") for t in tickets],
    }
