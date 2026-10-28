"""Identity access dependencies (ORM-user loaders + self/admin checks).

Relocated from ``identity/guards.py`` into the module's ``access/`` package (the
canonical home for module authorization). These remain dependency functions
rather than ``require(...)`` atoms because they return the loaded ORM ``User``
(``get_current_user``) or encode the 422 / "Access denied" semantics the
self-or-admin endpoints rely on. Pure role gates in the API now use
``require(Role("admin"))`` from ``sandloom.shared.authz`` directly.
"""

from collections.abc import Awaitable, Callable
from typing import get_args
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.security import decode_token
from sandloom.sdk.web import DbSession, authenticates, current_user_id
from sandloom_identity.models import Role, UserRole
from sandloom_identity.models.user import User
from sandloom_identity.schemas.user import Principal
from sandloom_identity.services import role_service
from sandloom_identity.sessions import is_token_revoked

#: The request unit's session dependency: exactly ``DbSession``'s
#: ``Depends`` (function scope), for guards whose session parameter follows
#: parameters with defaults.
_REQUEST_DB = get_args(DbSession)[1]

security = HTTPBearer(auto_error=False)


class AuthService:
    """The access token -> active user lookup of identity's own guards (a
    seam tests patch).

    Provides the same get_current_user(token, db) contract used in tests,
    internally decoding the JWT and loading the active user.
    """

    @classmethod
    async def get_current_user(cls, token: str, db: AsyncSession) -> User | None:
        payload = decode_token(token)
        if not payload:
            return None
        if payload.get("token_type") != "access":
            # Reject non-access tokens signed with the same key (refresh, etc.).
            return None
        user_id_str = payload.get("sub")
        if not user_id_str:
            return None
        result = await db.execute(select(User).where(User.id == UUID(user_id_str)))
        user = result.scalar_one_or_none()
        if user and user.is_active:
            return user
        return None


def _invalid_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _revoked(token: str) -> bool:
    """True when the access token predates a session revocation for its user."""
    claims = decode_token(token)
    return bool(claims) and await is_token_revoked(claims or {})


@authenticates
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = _REQUEST_DB,
) -> User:
    """Get current authenticated user (active, session not revoked)."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user = await AuthService.get_current_user(credentials.credentials, db)
        if not user:
            raise _invalid_token()
        if await _revoked(credentials.credentials):
            raise _invalid_token()
        return user
    except HTTPException as e:
        if e.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            raise  # fail-closed revocation-store outage: not a credential error
        raise _invalid_token() from e
    except (ValueError, TypeError) as e:
        # Malformed token subjects are credential errors. Database outages
        # (including the kernel's retryable DatabaseUnavailable/503) and bugs
        # must reach the error handler, never masquerade as a rejected login.
        raise _invalid_token() from e


@authenticates
async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = _REQUEST_DB,
) -> Principal:
    """Authenticated caller as a DTO (what identity's API layer consumes)."""
    user = await get_current_user(credentials, db)
    claims = decode_token(credentials.credentials) if credentials else None
    return Principal(
        id=user.id,
        email=user.email,
        has_password=bool(user.hashed_password),
        token_claims=claims or {},
    )


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = _REQUEST_DB,
) -> User | None:
    """Get current user if token is provided (optional authentication)."""
    if not credentials:
        return None

    try:
        user = await AuthService.get_current_user(credentials.credentials, db)
        if user is not None and await _revoked(credentials.credentials):
            return None
        return user
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        return None
    except (ValueError, TypeError):
        return None


def require_role(role_name: str) -> Callable[..., Awaitable[User]]:
    """Require specific role."""

    async def role_checker(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = _REQUEST_DB,
    ) -> User:
        has_role = await role_service.RoleService.user_has_role(
            current_user.id, role_name, db
        )
        if not has_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role_name}' required",
            )
        return current_user

    return role_checker


async def _user_has_admin_role(user_id: UUID, db: AsyncSession) -> bool:
    """Check if user has admin role."""
    result = await db.execute(
        select(UserRole)
        .join(Role)
        .where(UserRole.user_id == user_id, Role.code == "admin")
    )
    user_role = result.scalar_one_or_none()
    return user_role is not None


def require_admin_or_self(
    user_id_param: str = "user_id",
) -> Callable[..., Awaitable[User]]:
    """Require admin role or self access."""

    async def admin_or_self_checker(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = _REQUEST_DB,
    ) -> User:
        # Extract user_id from path parameters
        user_id = UUID(request.path_params[user_id_param])

        # Check if user is accessing their own resource or is admin
        if current_user.id != user_id:
            has_admin_role = await _user_has_admin_role(current_user.id, db)
            if not has_admin_role:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
                )

        return current_user

    return admin_or_self_checker


async def require_admin(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = _REQUEST_DB,
) -> User:
    """Require admin role."""
    has_admin_role = await _user_has_admin_role(current_user.id, db)
    if not has_admin_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return current_user


async def user_has_admin_role(user_id: UUID, db: AsyncSession) -> bool:
    """Public function to check if user has admin role."""
    return await _user_has_admin_role(user_id, db)


def is_self_or_admin(user_id_param: str = "user_id") -> Callable[..., Awaitable[User]]:
    """Check if user is accessing their own resource or is admin."""

    async def self_or_admin_checker(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = _REQUEST_DB,
    ) -> User:
        # Extract user_id from path parameters
        try:
            user_id = UUID(request.path_params[user_id_param])
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid user ID format",
            ) from exc

        # Check if user is accessing their own resource or is admin
        if current_user.id != user_id:
            has_admin_role = await _user_has_admin_role(current_user.id, db)
            if not has_admin_role:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
                )

        return current_user

    return self_or_admin_checker


async def admin_in_transaction(
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> UUID:
    """The caller holds ``admin`` AT COMMIT of this request (ADR-0003 G2).

    For identity's class-A mutations (RBAC, user administration), next to the
    claim-cheap ``require(Role("admin"))``: the claim is fresh as of the
    request start only, so this re-checks the role in the request's OWN Unit
    of Work (``DbSession`` — the handler's session) under the RBAC
    administration lock with the actor's assignment read ``FOR SHARE``
    (:meth:`RoleService.holds_admin_in_tx`). ``403`` when the role is gone.
    Identity owns RBAC, so this is a local read in every deployment shape.
    """
    await role_service.RoleService.require_admin_in_tx(actor_id, db)
    return actor_id
