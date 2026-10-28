"""Notifications read API — authenticated and tenant-bound by the router."""

from sandloom.sdk import module_router
from sandloom.sdk.data import PlannedReads  # sandloom: advanced read-only planner route

from ..schemas import NotificationOut
from ..services import NotificationQueryService

router = module_router()


@router.get("", response_model=list[NotificationOut])
async def list_notifications(reads: PlannedReads) -> list[NotificationOut]:
    """List the current tenant's in-app notifications.

    ``module_router()`` binds the tenant (X-Tenant-Id header / JWT claim;
    ``NotificationRecord`` is ``TenantScoped``); the declared read is then
    scoped to it — no manual tenant_id clause to forget. A read-only route
    (no Unit of Work).
    """
    return await NotificationQueryService.list_recent(reads)
