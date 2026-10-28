"""Routes of the invites module: ``/api/v1/invites/...``.

``module_router()`` authenticates every route and binds the tenant (the
caller's organization); ``OrgRole`` checks the membership live.
"""

from typing import Annotated

from fastapi import Depends, Query

from sandloom.sdk import CurrentUser, OrgRole, module_router, require

from . import services
from .schemas import InvitationIn, InvitationOut

router = module_router()

MEMBER = Depends(require(OrgRole("member")))


@router.post("/invitations", status_code=201, dependencies=[MEMBER])
async def send_invitation(body: InvitationIn, user: CurrentUser) -> InvitationOut:
    return await services.send_invitation(body, invited_by=user.user_id)


@router.get("/invitations", dependencies=[MEMBER])
async def list_invitations(
    user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[InvitationOut]:
    """A page of the organization's invitations (``le=`` caps one request)."""
    return await services.list_invitations(limit=limit)
