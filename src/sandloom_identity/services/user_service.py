"""User management service."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import Select, Text, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom_identity.events import (
    EventActor,
    create_user_deleted_event,
    create_user_updated_event,
)
from sandloom_identity.models import User
from sandloom_identity.schemas.user import UserListOut, UserOut, UserUpdate
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.sessions import revoke_sessions


class UserService:
    """User management service."""

    @classmethod
    async def _on_deactivated(
        cls, user: User, db: AsyncSession, actor: EventActor | None, how: str
    ) -> None:
        """Kill every session of a deactivated user and audit it."""
        await TokenService.revoke_all_user_tokens(user.id, db)
        actor_id = actor.user_id if actor else None
        await revoke_sessions(user.id, reason="deactivated", actor_id=actor_id)
        record_audit(
            "identity.user.deactivated",
            actor_id=actor_id,
            resource_type="user",
            resource_id=str(user.id),
            data={"via": how},
            required=True,
        )

    @classmethod
    def get_instance(cls) -> "UserService":
        """Get service instance."""
        return cls()

    @classmethod
    async def get_by_id(cls, user_id: UUID, db: AsyncSession) -> User:
        """Get user by ID.

        Loads the user row only: no caller (``UserOut``, the update/deactivate
        paths) reads ``user_roles``, and roles have their own endpoints
        (``RoleService.list_user_roles``). An eager ``selectinload`` of the
        roles here cost every ``GET /auth/me`` a second query.
        """
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        return user

    @classmethod
    async def get_by_email(cls, email: str, db: AsyncSession) -> User | None:
        """Get user by email."""
        result = await db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    @staticmethod
    def _user_query(search: str | None, is_active: bool | None) -> Select[User]:
        """Filtered user selection. ``search`` is a literal substring of name,
        email or username (the caller's ``%`` and ``_`` are not wildcards); the
        matching strategy can change (trigram or prefix index) without changing
        the API."""
        query = select(User)
        if search:
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            search_filter = f"%{escaped}%"
            query = query.where(
                (User.full_name.ilike(search_filter, escape="\\"))
                # ``email::text``: the same case-insensitive match as citext's
                # ILIKE, but servable by ix_users_search_trgm (pg_trgm).
                | (cast(User.email, Text).ilike(search_filter, escape="\\"))
                | (User.username.ilike(search_filter, escape="\\"))
            )
        if is_active is not None:
            query = query.where(User.is_active == is_active)
        return query

    @classmethod
    async def list_users(
        cls,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        search: str | None = None,
        is_active: bool | None = None,
        *,
        include_total: bool = True,
    ) -> UserListOut:
        """List users with filtering.

        ``include_total=False`` skips the ``COUNT(*)`` over the filter (a scan
        of every matching row; 55-71 ms on 100k users) and leaves ``total``
        ``None`` — for callers that page without showing a total.
        """
        total = await cls.count_users(db, search, is_active) if include_total else None
        users = await cls.list_page(db, skip, limit, search, is_active)
        return UserListOut(
            users=users,
            total=total,
            page=skip // limit + 1,
            per_page=limit,
        )

    @classmethod
    async def count_users(
        cls,
        db: AsyncSession,
        search: str | None = None,
        is_active: bool | None = None,
    ) -> int:
        """Number of users matching the filter (a scan of every match)."""
        query = cls._user_query(search, is_active)
        count_query = select(func.count()).select_from(query.subquery())
        return (await db.execute(count_query)).scalar() or 0

    @classmethod
    async def list_page(
        cls,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        search: str | None = None,
        is_active: bool | None = None,
    ) -> list[UserOut]:
        """One page of users, newest first, WITHOUT a COUNT (the operator API
        pages with ``limit + 1`` lookahead; a count costs a full scan)."""
        query = cls._user_query(search, is_active)
        result = await db.execute(
            query.order_by(User.created_at.desc(), User.id).offset(skip).limit(limit)
        )
        return [UserOut.model_validate(user) for user in result.scalars().all()]

    @classmethod
    async def update_user(
        cls,
        user_id: UUID,
        user_data: UserUpdate,
        db: AsyncSession,
        actor: EventActor | None = None,
        allow_is_active_change: bool = False,
    ) -> User:
        """Update user."""

        user = await cls.get_by_id(user_id, db)
        was_active = user.is_active

        # Update fields
        # Dump only provided fields and ignore explicit None values
        update_data = {
            key: value
            for key, value in user_data.model_dump(exclude_unset=True).items()
            if value is not None
        }
        # Only admins can change is_active; ignore otherwise
        if not allow_is_active_change:
            update_data.pop("is_active", None)
        try:
            async with db.begin_nested():
                for field, value in update_data.items():
                    setattr(user, field, value)
                user.updated_at = datetime.now(UTC)
                await db.flush()
        except IntegrityError as exc:
            if "username" not in update_data:
                raise
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username is already in use",
            ) from exc
        if was_active and update_data.get("is_active") is False:
            await cls._on_deactivated(user, db, actor, "update")
        elif not was_active and update_data.get("is_active") is True:
            cls._audit_activated(user, actor, "update")
        await db.refresh(user)

        # Publish event
        event = create_user_updated_event(
            user_id=user.id,
            fields=list(update_data.keys()),
            actor=actor or EventActor(),
        )
        await event.publish()
        return user

    @classmethod
    async def delete_user(
        cls, user_id: UUID, db: AsyncSession, actor: EventActor | None = None
    ) -> None:
        """Delete user (soft delete by deactivating)."""

        user = await cls.get_by_id(user_id, db)

        # Soft delete by deactivating
        user.is_active = False
        user.updated_at = datetime.now(UTC)
        await db.flush()
        await cls._on_deactivated(user, db, actor, "delete")

        # Publish event
        event = create_user_deleted_event(user_id=user.id, actor=actor or EventActor())
        await event.publish()

    @classmethod
    async def activate_user(
        cls, user_id: UUID, db: AsyncSession, actor: EventActor | None = None
    ) -> User:
        """Activate user."""

        user = await cls.get_by_id(user_id, db)
        user.is_active = True
        user.updated_at = datetime.now(UTC)
        await db.flush()
        await db.refresh(user)
        cls._audit_activated(user, actor, "activate")

        # Publish event
        event = create_user_updated_event(
            user_id=user.id, fields=["is_active"], actor=actor or EventActor()
        )
        await event.publish()
        return user

    @staticmethod
    def _audit_activated(user: User, actor: EventActor | None, how: str) -> None:
        record_audit(
            "identity.user.activated",
            actor_id=actor.user_id if actor else user.id,
            resource_type="user",
            resource_id=str(user.id),
            data={"via": how},
            required=True,
        )

    @classmethod
    async def deactivate_user(
        cls, user_id: UUID, db: AsyncSession, actor: EventActor | None = None
    ) -> User:
        """Deactivate user."""

        user = await cls.get_by_id(user_id, db)
        user.is_active = False
        user.updated_at = datetime.now(UTC)
        await db.flush()
        await cls._on_deactivated(user, db, actor, "deactivate")
        await db.refresh(user)

        # Publish event
        event = create_user_updated_event(
            user_id=user.id, fields=["is_active"], actor=actor or EventActor()
        )
        await event.publish()
        return user
