"""Contracts the stats module provides."""

from sandloom.sdk import contract

from . import services


# --8<-- [start:contract]
@contract("stats.quota.v1")
class Quota:
    """Contract ``stats.quota.v1``: public ``async def`` methods are reads,
    ``@command`` marks a command (deduplicated by its key, a saga step)."""

    async def may_create_item(self) -> bool:
        return await services.item_count() < services.ITEM_LIMIT


# --8<-- [end:contract]
