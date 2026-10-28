"""Routes of the activity module: ``/api/v1/activity/...``.

``module_router()`` authenticates every route and binds the tenant.
"""

from typing import Annotated

from fastapi import Depends, Query

from sandloom.sdk import CurrentUser, OrgRole, module_router, require

from . import services
from .schemas import EntryOut

router = module_router()


@router.get("/entries", dependencies=[Depends(require(OrgRole("member")))])
async def list_entries(
    user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[EntryOut]:
    """The organization's newest feed entries (``le=`` caps one request)."""
    return await services.list_entries(limit=limit)
