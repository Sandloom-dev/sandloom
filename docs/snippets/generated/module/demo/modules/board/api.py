"""Routes of the board module: ``/api/v1/board/...``.

``module_router()`` authenticates every route and binds the tenant.
"""

from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Query

from sandloom.sdk import CurrentUser, module_router
from sandloom.sdk.module import settings_of  # sandloom: advanced module settings

from . import services
from .schemas import ItemIn, ItemOut
from .settings import BoardSettings

router = module_router()


@router.post("/items", status_code=201)
async def create_item(body: ItemIn, user: CurrentUser) -> ItemOut:
    return await services.create_item(body, owner_id=user.user_id)


@router.get("/items")
async def list_items(
    user: CurrentUser, limit: Annotated[int | None, Query(ge=1, le=100)] = None
) -> list[ItemOut]:
    """A page of the tenant's items: ``le=`` caps what one request may load."""
    if limit is None:
        limit = settings_of(BoardSettings).default_page_size
    return await services.list_items(limit=limit)


@router.get("/items/{item_id}")
async def get_item(item_id: UUID, user: CurrentUser) -> ItemOut:
    item = await services.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item
