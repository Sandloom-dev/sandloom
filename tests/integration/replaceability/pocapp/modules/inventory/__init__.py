"""C: an unrelated fact ``inventory.stock.available`` (not billing).

Live-only: the owner's contract answers from its stock; the SKU comes from the
request's path parameter, the minimum quantity from ``?qty=`` (default 1).
"""

from typing import Any

from sandloom.sdk import define_module
from sandloom.sdk.authz import MODE_LIVE, AccessFact, FactUnavailable


async def _live(service: Any, ctx: Any) -> bool:
    request = ctx.request
    sku = (getattr(request, "path_params", None) or {}).get("sku")
    if sku is None:
        raise FactUnavailable("inventory.stock.available needs a {sku} path parameter")
    qty = int((getattr(request, "query_params", None) or {}).get("qty", 1))
    return bool(await service.available(str(sku), qty))


STOCK_AVAILABLE = AccessFact(
    name="inventory.stock.available",
    owner_module="inventory",
    live_service="inventory.stock.v1",
    live_resolver=_live,
    min_freshness=MODE_LIVE,
    default_mode=MODE_LIVE,
)


def _setup(api: Any) -> None:
    api.register_access_fact(STOCK_AVAILABLE)


register = define_module(__name__, version="0.1.0", setup=_setup)
