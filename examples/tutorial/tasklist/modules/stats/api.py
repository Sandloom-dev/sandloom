"""Routes of the stats module: ``/api/v1/stats/...``.

``module_router()`` authenticates every route and binds the tenant.
"""

from sandloom.sdk import CurrentUser, module_router

from . import services
from .schemas import CountOut

router = module_router()


# --8<-- [start:route]
@router.get("/count")
async def get_count(user: CurrentUser) -> CountOut:
    return CountOut(items=await services.item_count())


# --8<-- [end:route]
