"""A reader route: the module's settings, read per request."""

from pydantic import BaseModel

from kb_inventory.settings import InventorySettings
from sandloom.sdk.module import settings_of  # sandloom: advanced settings
from sandloom.sdk.web import module_router, public

router = module_router()


class InventoryConfigOut(BaseModel):
    reserve_ttl_seconds: int
    warehouse: str
    has_api_token: bool


@router.get(
    "/config",
    dependencies=[public(reason="fixture: reads the module's own settings")],
)
async def inventory_config() -> InventoryConfigOut:
    current = settings_of(InventorySettings)
    return InventoryConfigOut(
        reserve_ttl_seconds=current.reserve_ttl_seconds,
        warehouse=current.warehouse,
        has_api_token=current.api_token is not None,
    )
