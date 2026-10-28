"""User-Role assignment API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from sandloom.sdk import require
from sandloom.sdk.authz import Role
from sandloom.sdk.web import DbSession, current_user_id, get_client_ip
from sandloom_identity.access import admin_in_transaction
from sandloom_identity.events import EventActor
from sandloom_identity.services.role_service import RoleService

# ``session_not_revoked`` makes an admin's demotion/deactivation effective here
# immediately instead of when their (claims-carrying) access token expires.
user_roles_router = APIRouter(
    prefix="/api/v1",
    tags=["user-roles"],
    dependencies=[],
)

# === User-Role Assignments ===


def _actor(actor_id: UUID, request: Request) -> EventActor:
    ip = get_client_ip(request)
    return EventActor(user_id=actor_id, ip=None if ip == "unknown" else ip)


@user_roles_router.post(
    "/users/{user_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Role("admin"))), Depends(admin_in_transaction)],
)
async def assign_role_to_user(
    user_id: UUID,
    role_id: UUID,
    request: Request,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> None:
    """Assign role to user (admin only)."""
    await RoleService.assign_role(user_id, role_id, db, actor=_actor(actor_id, request))


@user_roles_router.delete(
    "/users/{user_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Role("admin"))), Depends(admin_in_transaction)],
)
async def revoke_role_from_user(
    user_id: UUID,
    role_id: UUID,
    request: Request,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> None:
    """Revoke role from user (admin only)."""
    await RoleService.revoke_role(user_id, role_id, db, actor=_actor(actor_id, request))
