"""Use cases of the profiles module."""

from uuid import UUID

from sandloom.sdk import db

from .models.profile import Profile
from .schemas import ProfileIn, ProfileOut


async def get_profile(user_id: UUID) -> ProfileOut | None:
    profile = await db().get(Profile, user_id)
    return ProfileOut(display_name=profile.display_name) if profile else None


async def save_profile(user_id: UUID, body: ProfileIn) -> ProfileOut:
    profile = await db().get(Profile, user_id)
    if profile is None:
        db().add(Profile(user_id=user_id, display_name=body.display_name))
    else:
        profile.display_name = body.display_name
    await db().flush()
    return ProfileOut(display_name=body.display_name)


async def delete_profile(user_id: UUID) -> None:
    profile = await db().get(Profile, user_id)
    if profile is not None:
        await db().delete(profile)
        await db().flush()
