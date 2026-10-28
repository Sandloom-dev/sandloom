"""User-facing invitation endpoints (not organization-scoped)."""

from uuid import UUID

from fastapi import APIRouter, Depends

from sandloom.sdk.web import DbSession, at_commit, current_user_id, sensitive
from sandloom_tenancy.access import TokenEmail, get_token_email
from sandloom_tenancy.api._errors import http_errors
from sandloom_tenancy.schemas import InvitationAcceptIn, MemberOut
from sandloom_tenancy.services import InvitationService

invitations_router = APIRouter(
    prefix="/api/v1/invitations",
    tags=["tenancy"],
    # ADR-0003 class A: accepting grants membership (tenancy-owned write).
)


@invitations_router.post(
    "/accept",
    response_model=MemberOut,
    # Class A whose authority (the verified e-mail claim) is identity's, not
    # tenancy's: re-read the caller's revocation / block marks right before
    # the membership commits (ADR-0003 G2, cross-service).
    dependencies=[at_commit(), sensitive("admin")],
)
async def accept_invitation(
    data: InvitationAcceptIn,
    db: DbSession,
    user_id: UUID = Depends(current_user_id),
    token_email: TokenEmail = Depends(get_token_email),
) -> MemberOut:
    """Accept an invitation token; the caller joins the organization.

    The caller's access-token e-mail must match the invited address (403
    otherwise); an invalid / expired / used token or a deleted organization is
    404; already being a member is 409. A failed attempt consumes nothing.
    """
    with http_errors():
        member = await InvitationService.accept(
            data.token,
            user_id,
            db,
            email=token_email.email,
            email_verified=token_email.verified,
        )
    return MemberOut.model_validate(member)
