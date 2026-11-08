"""The stock list the console's screen reads (never served in this test)."""

from fastapi import Query
from pydantic import BaseModel

from sandloom.sdk import CurrentUser, module_router

router = module_router()


class StockOut(BaseModel):
    id: str
    sku: str
    quantity: int


@router.get("/stock")
async def stock(
    user: CurrentUser,
    skip: int = 0,
    limit: int = Query(25, ge=1, le=101),
) -> list[StockOut]:
    return []
