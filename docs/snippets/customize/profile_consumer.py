"""Consumers of the profiles module."""

from typing import Any
from uuid import UUID

from sandloom.sdk import Event, on

from . import services


class UserDeleted(Event, channel="identity.user_deleted.v1"):
    """A view: the fields of identity's event this module reads."""

    payload: dict[str, Any]


@on(UserDeleted)
async def on_user_deleted(event: UserDeleted) -> None:
    await services.delete_profile(UUID(event.payload["user_id"]))
