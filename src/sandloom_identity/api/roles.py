"""Roles API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from sandloom.sdk import require
from sandloom.sdk.authz import Role
from sandloom.sdk.web import DbSession
from sandloom_identity.access import admin_in_transaction
from sandloom_identity.schemas.rbac import (
    PermissionCreate,
    PermissionOut,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    RoleWithPermissions,
)
from sandloom_identity.services.role_service import PermissionService, RoleService

# ``session_not_revoked``: a demoted/deactivated admin loses access here at once,
# not when their claims-carrying access token expires.
roles_router = APIRouter(
    prefix="/api/v1",
    tags=["roles"],
    dependencies=[],
)

# === Roles Management ===


@roles_router.post(
    "/roles",
    response_model=RoleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def create_role(
    role_data: RoleCreate,
    db: DbSession,
) -> RoleOut:
    """Create new role (admin only)."""
    role = await RoleService.create_role(role_data, db)
    return RoleOut.model_validate(role)


@roles_router.get(
    "/roles",
    response_model=list[RoleOut],
    dependencies=[Depends(require(Role("admin")))],
)
async def list_roles(
    db: DbSession,
) -> list[RoleOut]:
    """List all roles (admin only)."""
    roles = await RoleService.list_roles(db)
    return [RoleOut.model_validate(role) for role in roles]


@roles_router.get(
    "/roles/{role_id}",
    response_model=RoleWithPermissions,
    dependencies=[Depends(require(Role("admin")))],
)
async def get_role(
    role_id: UUID,
    db: DbSession,
) -> RoleWithPermissions:
    """Get role with permissions (admin only)."""
    role = await RoleService.get_role_by_id(role_id, db)
    return RoleWithPermissions.model_validate(role)


@roles_router.put(
    "/roles/{role_id}",
    response_model=RoleOut,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def update_role(
    role_id: UUID,
    role_data: RoleUpdate,
    db: DbSession,
) -> RoleOut:
    """Update role (admin only)."""
    role = await RoleService.update_role(role_id, role_data, db)
    return RoleOut.model_validate(role)


@roles_router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def delete_role(
    role_id: UUID,
    db: DbSession,
) -> None:
    """Delete role (admin only)."""
    await RoleService.delete_role(role_id, db)


# === Permissions Management ===


@roles_router.post(
    "/permissions",
    response_model=PermissionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def create_permission(
    permission_data: PermissionCreate,
    db: DbSession,
) -> PermissionOut:
    """Create new permission (admin only)."""
    permission = await PermissionService.create_permission(permission_data, db)
    return PermissionOut.model_validate(permission)


@roles_router.get(
    "/permissions",
    response_model=list[PermissionOut],
    dependencies=[Depends(require(Role("admin")))],
)
async def list_permissions(
    db: DbSession,
) -> list[PermissionOut]:
    """List all permissions (admin only)."""
    permissions = await PermissionService.list_permissions(db)
    return [PermissionOut.model_validate(perm) for perm in permissions]


@roles_router.post(
    "/roles/{role_id}/permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def assign_permission_to_role(
    role_id: UUID,
    permission_id: UUID,
    db: DbSession,
) -> None:
    """Assign permission to role (admin only)."""
    await PermissionService.assign_permission_to_role(role_id, permission_id, db)


@roles_router.delete(
    "/roles/{role_id}/permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require(Role("admin"))),
        Depends(admin_in_transaction),
    ],
)
async def revoke_permission_from_role(
    role_id: UUID,
    permission_id: UUID,
    db: DbSession,
) -> None:
    """Revoke permission from role (admin only)."""
    await PermissionService.revoke_permission_from_role(role_id, permission_id, db)
