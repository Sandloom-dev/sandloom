"""The board use cases. ``db()`` is the session of the running unit."""

from uuid import UUID

import sqlalchemy as sa

from sandloom.sdk import db

from .events import ItemCreated
from .models.item import BoardItem
from .schemas import ItemIn, ItemOut


async def create_item(body: ItemIn, *, owner_id: UUID) -> ItemOut:
    item = BoardItem(title=body.title, owner_id=owner_id)
    db().add(item)
    await db().flush()
    await ItemCreated(item_id=item.id, tenant_id=item.tenant_id).publish()
    return ItemOut.model_validate(item)


async def get_item(item_id: UUID) -> ItemOut | None:
    item = await db().get(BoardItem, item_id)
    return None if item is None else ItemOut.model_validate(item)


async def list_items(*, limit: int) -> list[ItemOut]:
    """The newest items first: ``created_at`` (the database clock), then the
    UUIDv7 ``id`` (creation order) for rows stamped in the same microsecond.
    ``TenantScoped`` keeps them to the bound tenant."""
    newest = BoardItem.created_at.desc()
    tie = BoardItem.id.desc()
    query = sa.select(BoardItem).order_by(newest, tie).limit(limit)
    rows = await db().scalars(query)
    return [ItemOut.model_validate(row) for row in rows]
