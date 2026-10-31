"""Routes of the profiles module: ``/api/v1/profiles/...``."""

from fastapi import HTTPException

from sandloom.sdk import CurrentUser, module_router

from . import services
from .schemas import ProfileIn, ProfileOut

router = module_router()


@router.get("/me")
async def read_my_profile(user: CurrentUser) -> ProfileOut:
    profile = await services.get_profile(user.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="No profile yet")
    return profile


@router.put("/me")
async def save_my_profile(body: ProfileIn, user: CurrentUser) -> ProfileOut:
    return await services.save_profile(user.user_id, body)
