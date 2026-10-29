"""The tasks use cases. ``db()`` is the session of the running unit."""

from uuid import UUID

from sqlalchemy import select

from sandloom.sdk import db

from .events import ItemCreated
from .models.item import TasksItem
from .schemas import ItemIn, ItemOut


async def create_item(body: ItemIn, *, owner_id: UUID) -> ItemOut:
    item = TasksItem(title=body.title, owner_id=owner_id)
    db().add(item)
    await db().flush()
    await ItemCreated(item_id=item.id, tenant_id=item.tenant_id).publish()
    return ItemOut.model_validate(item)


async def get_item(item_id: UUID) -> ItemOut | None:
    item = await db().get(TasksItem, item_id)
    return None if item is None else ItemOut.model_validate(item)


async def list_items(*, limit: int) -> list[ItemOut]:
    newest = TasksItem.created_at.desc()
    tie = TasksItem.id.desc()
    query = select(TasksItem).order_by(newest, tie).limit(limit)
    rows = await db().scalars(query)
    return [ItemOut.model_validate(row) for row in rows]
