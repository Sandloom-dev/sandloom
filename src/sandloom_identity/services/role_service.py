"""Role and permission management service."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sandloom.sdk import record_audit
from sandloom_identity.events import (
    EventActor,
    create_user_role_assigned_event,
    create_user_role_revoked_event,
)
from sandloom_identity.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from sandloom_identity.schemas.rbac import (
    PermissionCreate,
    RoleCreate,
    RoleUpdate,
)
from sandloom_identity.sessions import revoke_sessions

#: ``pg_advisory_xact_lock`` key of RBAC / user administration (ADR-0003 G2):
#: every class-A admin mutation takes it FIRST (``require_admin_in_tx``), so
#: they serialise in one global order — no deadlock between two admins acting
#: on each other — and each re-reads the actor's role after the previous one
#: committed. Admin mutations are rare; the lock is transaction-scoped.
RBAC_ADMIN_LOCK_KEY = 0x414A_4152_4241_4331  # "SANDLOOMRBAC1"

ADMIN_ROLE_CODE = "admin"


def _audit_rbac(
    action: str,
    actor: EventActor | None,
    resource_type: str,
    resource_id: UUID,
    data: dict[str, str],
) -> None:
    """Audit an RBAC mutation in the caller's UoW (commits iff it commits)."""
    record_audit(
        action,
        actor_id=actor.user_id if actor else None,
        resource_type=resource_type,
        resource_id=str(resource_id),
        data=data,
        required=True,
    )


class RoleService:
    """Role management service."""

    @classmethod
    def get_instance(cls) -> "RoleService":
        """Get service instance."""
        return cls()

    @classmethod
    async def create_role(
        cls, role_data: RoleCreate, db: AsyncSession, actor: EventActor | None = None
    ) -> Role:
        """Create new role."""

        # Check if role already exists
        result = await db.execute(
            select(Role).where(
                (Role.name == role_data.name) | (Role.code == role_data.code)
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role with this name or code already exists",
            )

        role = Role(
            name=role_data.name,
            code=role_data.code,
            description=role_data.description,
            is_system=False,
        )

        db.add(role)
        await db.flush()
        await db.refresh(role)

        _audit_rbac(
            "identity.role.created", actor, "role", role.id, {"role_code": role.code}
        )
        return role

    @classmethod
    async def get_role_by_id(cls, role_id: UUID, db: AsyncSession) -> Role:
        """Get role by ID."""
        result = await db.execute(
            select(Role)
            .options(
                selectinload(Role.role_permissions).selectinload(
                    RolePermission.permission
                )
            )
            .where(Role.id == role_id)
        )
        role = result.scalar_one_or_none()

        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
            )

        return role

    @classmethod
    async def get_role_by_code(cls, code: str, db: AsyncSession) -> Role | None:
        """Get role by code."""
        result = await db.execute(select(Role).where(Role.code == code))
        return result.scalar_one_or_none()

    @classmethod
    async def list_roles(cls, db: AsyncSession) -> list[Role]:
        """List all roles."""
        result = await db.execute(
            select(Role)
            .options(
                selectinload(Role.role_permissions).selectinload(
                    RolePermission.permission
                )
            )
            .order_by(Role.name)
        )
        return list(result.scalars().all())

    @classmethod
    async def list_user_roles(cls, user_id: UUID, db: AsyncSession) -> list[Role]:
        """List roles assigned to a user.

        Note: If the user doesn't exist or has no roles, returns an empty list.
        """
        result = await db.execute(
            select(Role).join(UserRole).where(UserRole.user_id == user_id)
        )
        return list(result.scalars().all())

    @classmethod
    async def update_role(
        cls,
        role_id: UUID,
        role_data: RoleUpdate,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> Role:
        """Update role."""

        role = await cls.get_role_by_id(role_id, db)

        if role.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot modify system role",
            )

        # Update fields
        update_data = role_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(role, field, value)

        role.updated_at = datetime.now(UTC)
        await db.flush()
        await db.refresh(role)

        _audit_rbac(
            "identity.role.updated",
            actor,
            "role",
            role.id,
            {"role_code": role.code, "fields": ",".join(sorted(update_data))},
        )
        return role

    @classmethod
    async def delete_role(
        cls, role_id: UUID, db: AsyncSession, actor: EventActor | None = None
    ) -> None:
        """Delete role."""

        role = await cls.get_role_by_id(role_id, db)

        if role.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete system role",
            )

        # Check if role is assigned to users
        result = await db.execute(
            select(UserRole).where(UserRole.role_id == role_id).limit(1)
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete role that is assigned to users",
            )

        role_code = role.code
        await db.delete(role)
        await db.flush()

        _audit_rbac(
            "identity.role.deleted", actor, "role", role_id, {"role_code": role_code}
        )

    @classmethod
    async def assign_role(
        cls,
        user_id: UUID,
        role_id: UUID,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> None:
        """Assign role to user."""

        # Check if user exists
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        # Check if role exists
        role_result = await db.execute(select(Role).where(Role.id == role_id))
        role = role_result.scalar_one_or_none()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
            )

        # Check if already assigned
        existing = await db.execute(
            select(UserRole).where(
                UserRole.user_id == user_id, UserRole.role_id == role_id
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role already assigned to user",
            )

        # Create assignment
        user_role = UserRole(
            user_id=user_id,
            role_id=role_id,
            assigned_by=actor.user_id if actor else None,
        )

        db.add(user_role)
        await db.flush()

        record_audit(
            "identity.role.assigned",
            actor_id=actor.user_id if actor else None,
            resource_type="user",
            resource_id=str(user_id),
            data={"role_code": role.code, "role_id": str(role_id)},
            required=True,
        )

        # Publish event
        event = create_user_role_assigned_event(
            user_id=user_id, role_code=role.code, actor=actor or EventActor()
        )
        await event.publish()

    @classmethod
    async def revoke_role(
        cls,
        user_id: UUID,
        role_id: UUID,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> None:
        """Revoke role from user."""

        # Find assignment
        result = await db.execute(
            select(UserRole)
            .options(selectinload(UserRole.role))
            .where(UserRole.user_id == user_id, UserRole.role_id == role_id)
        )
        user_role = result.scalar_one_or_none()

        if not user_role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role assignment not found",
            )

        role_code = user_role.role.code

        await db.delete(user_role)
        await db.flush()

        record_audit(
            "identity.role.revoked",
            actor_id=actor.user_id if actor else None,
            resource_type="user",
            resource_id=str(user_id),
            data={"role_code": role_code, "role_id": str(role_id)},
            required=True,
        )
        # Role claims are baked into access tokens: invalidate the user's
        # outstanding ones so the revocation is effective immediately on
        # identity's guards (refresh re-mints with the reduced claims).
        await revoke_sessions(
            user_id,
            reason="role_revoked",
            actor_id=actor.user_id if actor else None,
        )

        # Publish event
        event = create_user_role_revoked_event(
            user_id=user_id, role_code=role_code, actor=actor or EventActor()
        )
        await event.publish()

    @classmethod
    async def holds_admin_in_tx(cls, actor_id: UUID, db: AsyncSession) -> bool:
        """Does ``actor_id`` hold ``admin`` (and is active) AT COMMIT of ``db``?

        ADR-0003 G2 (class A): the ``Role("admin")`` claim is fresh only as of
        the request start. This takes the RBAC administration lock, then reads
        the actor's ``admin`` assignment and user row ``FOR SHARE`` in the
        caller's transaction: a revocation / deactivation of the actor either
        committed before (the read sees it) or waits until this transaction
        ends — never "checked as admin, demoted, acted anyway".
        """
        await db.execute(select(func.pg_advisory_xact_lock(RBAC_ADMIN_LOCK_KEY)))
        held = await db.execute(
            select(UserRole.id)
            .join(Role, Role.id == UserRole.role_id)
            .join(User, User.id == UserRole.user_id)
            .where(
                UserRole.user_id == actor_id,
                Role.code == ADMIN_ROLE_CODE,
                User.is_active.is_(True),
            )
            .with_for_update(of=(UserRole, User), read=True)
        )
        return held.first() is not None

    @classmethod
    async def require_admin_in_tx(cls, actor_id: UUID, db: AsyncSession) -> None:
        """:meth:`holds_admin_in_tx` or ``403``."""
        if not await cls.holds_admin_in_tx(actor_id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
            )

    @classmethod
    async def user_has_role(
        cls, user_id: UUID, role_code: str, db: AsyncSession
    ) -> bool:
        """Check if user has specific role."""
        result = await db.execute(
            select(UserRole)
            .join(Role)
            .where(UserRole.user_id == user_id, Role.code == role_code)
        )
        user_role = result.scalar_one_or_none()
        return user_role is not None


class PermissionService:
    """Permission management service."""

    @classmethod
    async def create_permission(
        cls,
        permission_data: PermissionCreate,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> Permission:
        """Create new permission."""

        # Check if permission already exists
        result = await db.execute(
            select(Permission).where(
                (Permission.name == permission_data.name)
                | (Permission.code == permission_data.code)
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Permission with this name or code already exists",
            )

        permission = Permission(
            name=permission_data.name,
            code=permission_data.code,
            description=permission_data.description,
            resource=permission_data.resource,
            action=permission_data.action,
            is_system=False,
        )

        db.add(permission)
        await db.flush()
        await db.refresh(permission)

        _audit_rbac(
            "identity.permission.created",
            actor,
            "permission",
            permission.id,
            {"permission_code": permission.code},
        )
        return permission

    @classmethod
    async def list_permissions(cls, db: AsyncSession) -> list[Permission]:
        """List all permissions."""
        result = await db.execute(select(Permission).order_by(Permission.name))
        return list(result.scalars().all())

    @classmethod
    async def assign_permission_to_role(
        cls,
        role_id: UUID,
        permission_id: UUID,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> None:
        """Assign permission to role."""

        # Check if role exists
        role_result = await db.execute(select(Role).where(Role.id == role_id))
        role = role_result.scalar_one_or_none()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
            )

        # Check if permission exists
        perm_result = await db.execute(
            select(Permission).where(Permission.id == permission_id)
        )
        permission = perm_result.scalar_one_or_none()
        if not permission:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Permission not found"
            )

        # Check if already assigned
        existing = await db.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Permission already assigned to role",
            )

        # Create assignment
        role_permission = RolePermission(role_id=role_id, permission_id=permission_id)

        db.add(role_permission)
        await db.flush()

        # Granting widens access only: holders pick the permission up on their
        # next refresh, no session needs ending.
        _audit_rbac(
            "identity.role.permission_assigned",
            actor,
            "role",
            role_id,
            _role_permission_data(role, permission),
        )

    @classmethod
    async def revoke_permission_from_role(
        cls,
        role_id: UUID,
        permission_id: UUID,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> None:
        """Revoke permission from role."""

        # Find assignment
        result = await db.execute(
            select(RolePermission)
            .options(
                selectinload(RolePermission.role),
                selectinload(RolePermission.permission),
            )
            .where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission_id,
            )
        )
        role_permission = result.scalar_one_or_none()

        if not role_permission:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Permission assignment not found",
            )

        data = _role_permission_data(role_permission.role, role_permission.permission)
        await db.delete(role_permission)
        await db.flush()

        _audit_rbac("identity.role.permission_revoked", actor, "role", role_id, data)
        # Permissions are baked into access-token claims: end the outstanding
        # sessions of every holder of the role, or a token minted before the
        # revocation keeps the permission until it expires (same rule as
        # revoking the role itself).
        holders = await db.execute(
            select(UserRole.user_id).where(UserRole.role_id == role_id)
        )
        for holder_id in holders.scalars().all():
            await revoke_sessions(
                holder_id,
                reason="role_permission_revoked",
                actor_id=actor.user_id if actor else None,
            )


def _role_permission_data(role: Role, permission: Permission) -> dict[str, str]:
    return {
        "role_id": str(role.id),
        "role_code": role.code,
        "permission_id": str(permission.id),
        "permission_code": permission.code,
    }
