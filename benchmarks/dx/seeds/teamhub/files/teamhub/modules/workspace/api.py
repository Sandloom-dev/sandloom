"""Routes of the workspace module: ``/api/v1/workspace/...``.

``module_router()`` authenticates every route and binds the tenant (the
caller's organization); ``OrgRole`` checks the membership live.
"""

from fastapi import Depends

from sandloom.sdk import CurrentUser, OrgRole, module_router, require

from . import services
from .schemas import SettingsIn, SettingsOut

router = module_router()


@router.get("/settings", dependencies=[Depends(require(OrgRole("member")))])
async def get_settings(user: CurrentUser) -> SettingsOut:
    return await services.get_settings()


@router.put("/settings", dependencies=[Depends(require(OrgRole("admin")))])
async def update_settings(body: SettingsIn, user: CurrentUser) -> SettingsOut:
    return await services.update_settings(body, user_id=user.user_id)
