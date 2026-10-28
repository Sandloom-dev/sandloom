"""Tenancy access: organization-role enforcement.

Authentication uses the canonical shared guard (`current_user_id`, a
projection of `sandloom.shared.authz.current_context`); the org-role check goes through
the
`tenancy.membership.v1` service contract — the same path the shared `OrgRole`
atom uses. `require_org_role` is kept as a dependency (rather than the
`require(OrgRole(...))` atom) only because it returns **404** for non-members to
avoid leaking organization existence — a distinction the single-boolean atom
cannot express.

The role is resolved LIVE from the membership table on every request (never
from the JWT ``tenant_ids`` claim), so a removed member or a deleted
organization is denied immediately.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import get_args
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.data import PlannedReads
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.security import decode_token_cached
from sandloom.sdk.web import DbSession, current_user_id
from sandloom_tenancy.roles import role_at_least
from sandloom_tenancy.services.membership_service import MembershipService
from sandloom_tenancy.services.operations import member_role_in_active_org

#: The request unit's session dependency: exactly ``DbSession``'s
#: ``Depends`` (function scope), for guards whose session parameter follows
#: parameters with defaults.
_REQUEST_DB = get_args(DbSession)[1]

__all__ = [
    "require_org_role",
    "require_org_role_reads",
    "current_user_id",
    "TokenEmail",
    "get_token_email",
]

_bearer = HTTPBearer(auto_error=False)


def _admin_tagged(
    guard: Callable[..., Awaitable[UUID]], minimum: str
) -> Callable[..., Awaitable[UUID]]:
    """An admin-or-owner gate carries the admin risk (ADR-0009 item 6)."""
    if minimum == "member":
        return guard
    return sensitive_effect("admin")(guard)


def require_org_role(minimum: str) -> Callable[..., Awaitable[UUID]]:
    """Dependency factory: caller must hold at least `minimum` role in the org.

    Reads `org_id` from the path. Returns the caller's user id on success.
    404 is returned for non-members (and for inactive organizations) to avoid
    leaking organization existence.
    """
    # Validate eagerly: a typo'd role must fail at import, not grant access.
    role_at_least(minimum, minimum)

    async def _guard(
        org_id: UUID,
        user_id: UUID = Depends(current_user_id),
        db: AsyncSession = _REQUEST_DB,
    ) -> UUID:
        role = await MembershipService.get_role(org_id, user_id, db)
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )
        if not role_at_least(role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient organization role",
            )
        return user_id

    return _admin_tagged(_guard, minimum)


def require_org_role_reads(minimum: str) -> Callable[..., Awaitable[UUID]]:
    """:func:`require_org_role` for a READ-ONLY route (``PlannedReads``).

    The same check (404 for non-members / inactive organizations, 403 below
    ``minimum``) through the declared ``member_role_in_active_org`` on the
    route's own ``PlannedReads`` handle (FastAPI shares it with the handler),
    so guard + handler reads share one read-only transaction on the general
    executor, or run on the optimized one. No Unit of Work is opened.
    """
    role_at_least(minimum, minimum)

    async def _guard(
        org_id: UUID,
        reads: PlannedReads,
        user_id: UUID = Depends(current_user_id),
    ) -> UUID:
        found = await reads.one(
            member_role_in_active_org, organization_id=org_id, user_id=user_id
        )
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )
        if not role_at_least(found.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient organization role",
            )
        return user_id

    return _admin_tagged(_guard, minimum)


@dataclass(frozen=True, slots=True)
class TokenEmail:
    """The caller's e-mail as asserted by the (signature-verified) access token."""

    email: str | None
    #: ``None`` when the token does not carry an ``email_verified`` claim.
    verified: bool | None


async def get_token_email(
    request: Request,
    _user_id: UUID = Depends(current_user_id),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> TokenEmail:
    """Read ``email`` / ``email_verified`` claims from the caller's access token.

    Depends on ``current_user_id`` so the token is already authenticated
    (and typed ``access``); the decode is cached per request.
    """
    payload = (
        decode_token_cached(request, credentials.credentials) if credentials else None
    ) or {}
    email = payload.get("email")
    verified = payload.get("email_verified")
    return TokenEmail(
        email=email if isinstance(email, str) and email else None,
        verified=verified if isinstance(verified, bool) else None,
    )
