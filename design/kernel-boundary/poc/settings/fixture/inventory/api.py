from fastapi import APIRouter

from sandloom.sdk.web import public

from sandloom.sdk.module import settings_of

from .settings import InventorySettings

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("/config", dependencies=[public(reason="KB-49 fixture probe")])
async def config() -> dict[str, object]:
    cfg = settings_of(InventorySettings)
    return {"reserve_ttl_seconds": cfg.reserve_ttl_seconds, "warehouse": cfg.warehouse}
