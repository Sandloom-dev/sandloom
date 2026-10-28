"""Routes of the tasks module: ``/api/v1/tasks/...``.

``module_router()`` authenticates every route and binds the tenant.
"""

from uuid import UUID

from fastapi import HTTPException

from sandloom.sdk import Contract, CurrentUser, module_router
from tasklist.shared.contracts.stats_quota_v1 import STATS_QUOTA_V1

from . import services
from .schemas import ItemIn, ItemOut

# --8<-- [start:quota]
# Required: items must never be created without the quota check.
quota = Contract(STATS_QUOTA_V1)
# --8<-- [end:quota]
router = module_router()


# --8<-- [start:routes]
@router.post("/items", status_code=201)
async def create_item(body: ItemIn, user: CurrentUser) -> ItemOut:
    if not await quota.may_create_item():
        raise HTTPException(status_code=402, detail="item quota reached")
    return await services.create_item(body, owner_id=user.user_id)


@router.get("/items/{item_id}")
async def get_item(item_id: UUID, user: CurrentUser) -> ItemOut:
    item = await services.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


# --8<-- [end:routes]
