"""The inventory module's own contract (what its live fact calls)."""

from sandloom.sdk.contracts import contract

STOCK = {"sku-in": 5, "sku-out": 0}


@contract("inventory.stock.v1")
class Stock:
    async def available(self, sku: str, qty: int) -> bool:
        return STOCK.get(sku, 0) >= qty
