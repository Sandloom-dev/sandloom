"""Users API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from sandloom.sdk import require
from sandloom.sdk.authz import Role
from sandloom.sdk.web import DbSession, current_user_id, sensitive
from sandloom_identity.access import (
    admin_in_transaction,
    is_self_or_admin,
    user_has_admin_role,
)
from sandloom_identity.events import EventActor
from sandloom_identity.schemas.rbac import RoleOut
from sandloom_identity.schemas.user import UserOut, UserUpdate
from sandloom_identity.services.role_service import RoleService
from sandloom_identity.services.user_service import UserService

# ``session_not_revoked``: revoked sessions (deactivation, role revoke, password
# reset) are rejected here immediately, not at access-token expiry.
users_router = APIRouter(
    prefix="/api/v1",
    tags=["users"],
    dependencies=[],
)

# === Users Management ===

#: The list's total (``include_total``); the body stays a bare list.
TOTAL_COUNT_HEADER = "X-Total-Count"


@users_router.get(
    "/users",
    response_model=list[UserOut],
    dependencies=[Depends(require(Role("admin")))],
)
async def list_users(
    db: DbSession,
    response: Response,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=100,
        description=(
            "Case-insensitive literal substring of full name, email or "
            "username (3+ characters use the trigram index)"
        ),
    ),
    is_active: bool | None = None,
    include_total: bool = Query(
        default=True,
        description=(
            "Count every matching user into the X-Total-Count header; false "
            "skips that COUNT (cheaper paging)"
        ),
    ),
) -> list[UserOut]:
    """List users (admin only); the total, when asked for, is in ``X-Total-Count``."""
    if include_total:
        response.headers[TOTAL_COUNT_HEADER] = str(
            await UserService.count_users(db, search=q, is_active=is_active)
        )
    return await UserService.list_page(
        db=db, skip=skip, limit=limit, search=q, is_active=is_active
    )


@users_router.get(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(is_self_or_admin())],
)
async def get_user_by_id(
    user_id: UUID,
    db: DbSession,
) -> UserOut:
    """Get user by ID (self or admin)."""
    user = await UserService.get_by_id(user_id, db)
    return UserOut.model_validate(user)


@users_router.put(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(is_self_or_admin()), sensitive("admin")],
)
async def update_user(
    user_id: UUID,
    user_data: UserUpdate,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> UserOut:
    """Update user (self or admin)."""
    # Administering ANOTHER user, or changing ``is_active``, is class A
    # (ADR-0003 G2): the admin role is re-checked in this transaction, under
    # the RBAC administration lock. A plain self-update needs no admin.
    # A self-update touching ``is_active`` takes the (global) RBAC lock only
    # when a cheap, lock-free read says the caller is an admin at all.
    is_admin = False
    wants_admin = user_id != actor_id or (
        "is_active" in user_data.model_fields_set
        and await user_has_admin_role(actor_id, db)
    )
    if wants_admin:
        is_admin = await RoleService.holds_admin_in_tx(actor_id, db)
        if user_id != actor_id and not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
            )

    updated = await UserService.update_user(
        user_id=user_id,
        user_data=user_data,
        db=db,
        actor=EventActor(user_id=actor_id),
        allow_is_active_change=is_admin,
    )
    return UserOut.model_validate(updated)


@users_router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def delete_user(
    user_id: UUID,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> None:
    """Soft delete user (admin only)."""
    await UserService.delete_user(
        user_id,
        db,
        actor=EventActor(user_id=actor_id),
    )


@users_router.get(
    "/users/{user_id}/roles",
    response_model=list[RoleOut],
    dependencies=[Depends(is_self_or_admin())],
)
async def get_user_roles(
    user_id: UUID,
    db: DbSession,
) -> list[RoleOut]:
    """Get user roles (self or admin)."""
    roles = await RoleService.list_user_roles(user_id, db)
    return [RoleOut.model_validate(role) for role in roles]
