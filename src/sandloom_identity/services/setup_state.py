"""First-run state: whether any platform administrator exists."""

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.role import Role
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole
from sandloom_identity.services.role_service import ADMIN_ROLE_CODE


async def platform_admin_exists(db: AsyncSession) -> bool:
    """Whether any active account holds the platform ``admin`` role."""
    found = await db.scalar(
        select(
            exists().where(
                UserRole.user_id == User.id,
                UserRole.role_id == Role.id,
                Role.code == ADMIN_ROLE_CODE,
                User.is_active.is_(True),
            )
        )
    )
    return bool(found)


__all__ = ["platform_admin_exists"]
