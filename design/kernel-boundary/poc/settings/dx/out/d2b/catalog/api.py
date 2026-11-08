from fastapi import APIRouter

from catalog.settings import CatalogSettings
from sandloom.sdk.web import public

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/info", dependencies=[public(reason="DX task probe")])
async def info() -> dict[str, object]:
    return {"page_size": CatalogSettings.current().page_size}
