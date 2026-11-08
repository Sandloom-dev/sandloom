from fastapi import APIRouter

from sandloom.sdk.module import settings_of
from sandloom.sdk.web import public

from catalog.settings import CatalogSettings

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/info", dependencies=[public(reason="DX task probe")])
async def info() -> dict[str, object]:
    return {"page_size": settings_of(CatalogSettings).page_size}
