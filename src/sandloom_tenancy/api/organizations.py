"""Organization management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from sandloom.sdk import on_commit
from sandloom.sdk.data import PlannedReads
from sandloom.sdk.web import DbSession, current_user_id, idempotency_guard, sensitive
from sandloom_tenancy.access import require_org_role, require_org_role_reads
from sandloom_tenancy.api._errors import http_errors
from sandloom_tenancy.exceptions import TenancyNotFoundError
from sandloom_tenancy.roles import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER
from sandloom_tenancy.schemas import (
    InvitationCreateIn,
    InvitationOut,
    InvitationTokenOut,
    MemberOut,
    MemberRoleUpdateIn,
    OrganizationCreateIn,
    OrganizationOut,
    OrganizationUpdateIn,
)
from sandloom_tenancy.schemas.membership import MemberRoleOut
from sandloom_tenancy.services import (
    InvitationService,
    MembershipService,
    OrganizationService,
)
from sandloom_tenancy.services.listing import TenancyLists
from sandloom_tenancy.services.operations import member_role_in_active_org, org_by_id

organizations_router = APIRouter(
    prefix="/api/v1/orgs",
    tags=["tenancy"],
    dependencies=[],
)


# === Organizations ===


@organizations_router.post(
    "",
    response_model=OrganizationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    data: OrganizationCreateIn,
    db: DbSession,
    user_id: UUID = Depends(current_user_id),
    _key: str | None = Depends(
        idempotency_guard(scope="tenancy.org.create", fail_closed=True, required=False)
    ),
) -> OrganizationOut:
    """Create an organization; the caller becomes its owner."""
    with http_errors():
        org = await OrganizationService.create(data, owner_id=user_id, db=db)
    return OrganizationOut.model_validate(org)


#: Upper bound of one page of a tenancy list endpoint.
MAX_PAGE_SIZE = 500
#: Response header carrying the next page's opaque cursor (absent on the last).
NEXT_CURSOR_HEADER = "X-Next-Cursor"
_CURSOR = Query(
    None,
    max_length=512,
    description=f"opaque cursor from the previous page's {NEXT_CURSOR_HEADER}",
)


@organizations_router.get(
    "",
    response_model=list[OrganizationOut],
)
async def list_my_organizations(
    response: Response,
    reads: PlannedReads,
    user_id: UUID = Depends(current_user_id),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = _CURSOR,
) -> list[OrganizationOut]:
    """List active organizations the caller belongs to (creation order).

    Keyset pagination: ``limit`` (<= 500) and the opaque ``cursor`` returned in
    the ``X-Next-Cursor`` header of the previous page. Without ``limit`` a
    bounded page of 100 organizations is returned. A read-only route.
    """
    page = await TenancyLists.organizations_of(
        reads, user_id, limit=limit, cursor=cursor
    )
    if page.next_cursor is not None:
        response.headers[NEXT_CURSOR_HEADER] = page.next_cursor
    return page.items


@organizations_router.get(
    "/{org_id}",
    response_model=OrganizationOut,
)
async def get_organization(
    org_id: UUID,
    reads: PlannedReads,
    user_id: UUID = Depends(require_org_role_reads(ROLE_MEMBER)),
) -> OrganizationOut:
    """Get organization details (members only; a read-only route)."""
    org = await reads.one(org_by_id, id=org_id)
    if org is None or not org.is_active:
        with http_errors():
            raise TenancyNotFoundError("Organization not found")
    return org  # type: ignore[no-any-return]


@organizations_router.patch(
    "/{org_id}",
    response_model=OrganizationOut,
)
async def update_organization(
    org_id: UUID,
    data: OrganizationUpdateIn,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_ADMIN)),
    _key: str | None = Depends(
        idempotency_guard(scope="tenancy.org.update", fail_closed=True, required=False)
    ),
) -> OrganizationOut:
    """Update organization attributes (admin or owner).

    Only ``name`` is mutable; deactivation is the owner-only DELETE.
    """
    with http_errors():
        org = await OrganizationService.update(org_id, data, db, actor_id=user_id)
    return OrganizationOut.model_validate(org)


@organizations_router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_organization(
    org_id: UUID,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_OWNER)),
    _key: str | None = Depends(
        idempotency_guard(scope="tenancy.org.delete", fail_closed=True, required=False)
    ),
) -> None:
    """Soft-delete organization (owner only)."""
    with http_errors():
        await OrganizationService.soft_delete(org_id, db, actor_id=user_id)


@organizations_router.post(
    "/{org_id}/transfer-ownership/{new_owner_id}",
    response_model=OrganizationOut,
)
async def transfer_ownership(
    org_id: UUID,
    new_owner_id: UUID,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_OWNER)),
    _key: str | None = Depends(
        idempotency_guard(
            scope="tenancy.org.transfer", fail_closed=True, required=False
        )
    ),
) -> OrganizationOut:
    """Transfer ownership to another member (owner only)."""
    with http_errors():
        org = await OrganizationService.transfer_ownership(
            org_id, new_owner_id, db, actor_id=user_id
        )
    return OrganizationOut.model_validate(org)


# === Members ===


@organizations_router.get(
    "/{org_id}/membership",
    response_model=MemberRoleOut,
)
async def get_my_membership(
    org_id: UUID,
    reads: PlannedReads,
    user_id: UUID = Depends(current_user_id),
) -> MemberRoleOut:
    """The caller's live role in an active org; non-members see only 404.

    This is a display hint for clients, never a substitute for authorizing
    subsequent commands in their own transaction.
    """
    found = await reads.one(
        member_role_in_active_org, organization_id=org_id, user_id=user_id
    )
    if found is None:
        with http_errors():
            raise TenancyNotFoundError("Organization not found")
    return MemberRoleOut.model_validate(found)


@organizations_router.get(
    "/{org_id}/members",
    response_model=list[MemberOut],
)
async def list_members(
    org_id: UUID,
    response: Response,
    reads: PlannedReads,
    user_id: UUID = Depends(require_org_role_reads(ROLE_MEMBER)),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = _CURSOR,
) -> list[MemberOut]:
    """List organization members (members only; join order).

    Keyset pagination: ``limit`` (<= 500) and the opaque ``cursor`` from the
    previous page's ``X-Next-Cursor`` header. Without ``limit``, the page
    defaults to 100 members. A read-only route (declared operations).
    """
    page = await TenancyLists.members_of(reads, org_id, limit=limit, cursor=cursor)
    if page.next_cursor is not None:
        response.headers[NEXT_CURSOR_HEADER] = page.next_cursor
    return page.items


@organizations_router.patch(
    "/{org_id}/members/{member_user_id}",
    response_model=MemberOut,
)
async def change_member_role(
    org_id: UUID,
    member_user_id: UUID,
    data: MemberRoleUpdateIn,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_ADMIN)),
    _key: str | None = Depends(
        idempotency_guard(scope="tenancy.member.role", fail_closed=True, required=False)
    ),
) -> MemberOut:
    """Change a member's role (admin or owner; only the owner may change admins)."""
    with http_errors():
        member = await MembershipService.change_role(
            org_id, member_user_id, data.role, db, actor_id=user_id
        )
    return MemberOut.model_validate(member)


@organizations_router.delete(
    "/{org_id}/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[sensitive("admin")],
)
async def remove_member(
    org_id: UUID,
    member_user_id: UUID,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_MEMBER)),
    _key: str | None = Depends(
        idempotency_guard(
            scope="tenancy.member.remove", fail_closed=True, required=False
        )
    ),
) -> None:
    """Remove a member, or leave the organization (``member_user_id`` = self).

    Any member may leave; removing someone else requires admin, and removing an
    admin requires the owner. The owner can neither leave nor be removed —
    transfer ownership first. The service enforces these rules.
    """
    with http_errors():
        await MembershipService.remove_member(
            org_id, member_user_id, db, actor_id=user_id
        )


# === Invitations (org-scoped) ===


@organizations_router.post(
    "/{org_id}/invitations",
    response_model=InvitationTokenOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    org_id: UUID,
    data: InvitationCreateIn,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_ADMIN)),
    _key: str | None = Depends(
        idempotency_guard(
            scope="tenancy.invitation.create", fail_closed=True, required=False
        )
    ),
) -> InvitationTokenOut:
    """Invite a user by email (admin or owner; only the owner may invite an admin).

    The invitee is e-mailed the accept link (after the transaction commits);
    the raw token is also returned once here and never stored.
    """
    with http_errors():
        org = await OrganizationService.get_or_404(org_id, db)
        created = await InvitationService.create(org_id, data, user_id, db)
    invitation = created.invitation
    email, token, name, invitation_id = (
        invitation.email,
        created.token,
        org.name,
        invitation.id,
    )

    async def _notify() -> None:
        await InvitationService.send_invitation_email(
            email=email,
            token=token,
            organization_id=org_id,
            organization_name=name,
            invitation_id=invitation_id,
        )

    await on_commit(_notify)
    return InvitationTokenOut(
        **InvitationOut.model_validate(invitation).model_dump(), token=created.token
    )


@organizations_router.get(
    "/{org_id}/invitations",
    response_model=list[InvitationOut],
)
async def list_invitations(
    org_id: UUID,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_ADMIN)),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    skip: int = Query(0, ge=0),
) -> list[InvitationOut]:
    """List pending invitations (admin or owner)."""
    pending = await InvitationService.list_pending(org_id, db, limit=limit, skip=skip)
    return [InvitationOut.model_validate(i) for i in pending]


@organizations_router.delete(
    "/{org_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    org_id: UUID,
    invitation_id: UUID,
    db: DbSession,
    user_id: UUID = Depends(require_org_role(ROLE_ADMIN)),
    _key: str | None = Depends(
        idempotency_guard(
            scope="tenancy.invitation.revoke", fail_closed=True, required=False
        )
    ),
) -> None:
    """Revoke a pending invitation (admin or owner)."""
    with http_errors():
        await InvitationService.revoke(org_id, invitation_id, db, actor_id=user_id)
