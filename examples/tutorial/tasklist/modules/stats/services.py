"""The stats use cases. ``db()`` is the session of the running unit."""

from sqlalchemy import select

from sandloom.sdk import db

from .models.counter import StatsItemCount

#: How many items one tenant may create (read by the quota contract).
ITEM_LIMIT = 100


# --8<-- [start:services]
async def _counter() -> StatsItemCount | None:
    # TenantScoped rows are filtered by the bound tenant: at most one row.
    result = await db().execute(select(StatsItemCount))
    return result.scalar_one_or_none()


async def record_item() -> None:
    counter = await _counter()
    if counter is None:
        db().add(StatsItemCount(items=1))
    else:
        counter.items += 1
    await db().flush()


async def item_count() -> int:
    counter = await _counter()
    return 0 if counter is None else counter.items


# --8<-- [end:services]
