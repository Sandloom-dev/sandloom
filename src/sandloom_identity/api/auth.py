"""Authentication API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from sandloom.sdk.data import PlannedReads
from sandloom.sdk.infra import CoreRuntime, get_core
from sandloom.sdk.web import (
    DbSession,
    current_user_id,
    get_client_ip,
    public,
    rate_limiter,
)
from sandloom_identity.errors import commit_on_security_error
from sandloom_identity.events import EventActor
from sandloom_identity.schemas.auth import (
    EmailVerificationIn,
    EmailVerificationResendIn,
    LoginIn,
    PasswordChangeIn,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    RefreshTokenIn,
    RegisterIn,
    RegistrationAcceptedOut,
    TokenPair,
)
from sandloom_identity.schemas.user import UserOut, UserUpdate
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.services.operations import user_by_id
from sandloom_identity.services.user_service import UserService

auth_router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[])


def _ip(request: Request) -> str | None:
    """The real client IP (proxy-aware, see ``TRUSTED_PROXY_IPS``) for audit."""
    ip = get_client_ip(request)
    return None if ip == "unknown" else ip


# === Basic Authentication ===


@auth_router.post(
    "/register",
    response_model=RegistrationAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        public(reason="sign-up: the caller has no account yet"),
    ],
)
async def register(
    user_data: RegisterIn,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limiter(requests=10, period=3600)),
) -> RegistrationAcceptedOut:
    """Register a new user.

    Always ``202`` with the same body — whether the address was new or already
    registered — so the endpoint cannot enumerate accounts. The mailbox owner
    receives a verification link (new) or an "account exists" notice.
    """
    await AuthService.register(user_data, db, actor=EventActor(ip=_ip(request)))
    return RegistrationAcceptedOut()


@auth_router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[
        public(
            reason="password login: the caller has no token yet", issues_tokens=True
        ),
    ],
)
@commit_on_security_error
async def login(
    credentials: LoginIn,
    request: Request,
    db: DbSession,
    core: CoreRuntime = Depends(get_core),
    _: None = Depends(rate_limiter(requests=5, period=60, fail_closed=True)),
) -> TokenPair:
    """Login user (password + 2FA ``otp`` when enabled)."""
    _user, tokens = await AuthService.login(
        credentials, db, actor=EventActor(ip=_ip(request)), core=core
    )
    return tokens


@auth_router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[
        public(
            reason="refresh-token exchange: the refresh token is the credential",
            issues_tokens=True,
        ),
    ],
)
@commit_on_security_error
async def refresh(
    data: RefreshTokenIn,
    db: DbSession,
    core: CoreRuntime = Depends(get_core),
    _: None = Depends(rate_limiter(requests=30, period=60)),
) -> TokenPair:
    """Rotate the refresh token (reuse revokes the whole token family)."""
    return await AuthService.refresh_token(data.refresh_token, db, core)


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        public(reason="logout: the refresh token in the body is the credential"),
    ],
)
async def logout(
    data: RefreshTokenIn,
    request: Request,
    db: DbSession,
) -> None:
    """Logout user."""
    await AuthService.logout(data.refresh_token, db, actor=EventActor(ip=_ip(request)))
    return None


# === Email Verification & Password Reset ===


@auth_router.post(
    "/verify-email",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        public(reason="e-mail verification: the signed link token is the credential"),
    ],
)
async def verify_email(
    data: EmailVerificationIn,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limiter(requests=5, period=60)),
) -> None:
    """Verify email address."""
    await AuthService.confirm_email(data.token, db, actor=EventActor(ip=_ip(request)))
    return None


@auth_router.post(
    "/resend-verification",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        public(reason="unverified accounts cannot log in yet"),
    ],
)
async def resend_verification(
    data: EmailVerificationResendIn,
    db: DbSession,
    _: None = Depends(rate_limiter(requests=3, period=3600, fail_closed=True)),
) -> None:
    """Email a fresh verification link (always 204, no enumeration)."""
    await AuthService.resend_verification(str(data.email), db)
    return None


@auth_router.post(
    "/request-password-reset",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        public(reason="password reset: the caller cannot log in"),
    ],
)
async def request_password_reset(
    data: PasswordResetRequestIn,
    db: DbSession,
    _: None = Depends(rate_limiter(requests=5, period=60, fail_closed=True)),
) -> None:
    """Request password reset (always 204, no enumeration)."""
    await AuthService.forgot_password(str(data.email), db)
    return None


@auth_router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        public(reason="password reset: the reset token is the credential"),
    ],
)
async def reset_password(
    data: PasswordResetConfirmIn,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limiter(requests=5, period=60, fail_closed=True)),
) -> None:
    """Reset password with a single-use emailed token."""
    await AuthService.reset_password(
        data.token, data.new_password, db, actor=EventActor(ip=_ip(request))
    )
    return None


@auth_router.post(
    "/change-password",
    response_model=TokenPair,
)
async def change_password(
    data: PasswordChangeIn,
    request: Request,
    db: DbSession,
    core: CoreRuntime = Depends(get_core),
    user_id: UUID = Depends(current_user_id),
    _: None = Depends(rate_limiter(requests=5, period=60, fail_closed=True)),
) -> TokenPair:
    """Change password (requires the current one).

    Revokes all of the user's sessions and returns a fresh token pair.
    """
    return await AuthService.change_password(
        user_id,
        data.current_password,
        data.new_password,
        db,
        actor=EventActor(user_id=user_id, ip=_ip(request)),
        core=core,
    )


# === User Profile ===


@auth_router.get(
    "/me",
    response_model=UserOut,
)
async def get_current_user_info(
    reads: PlannedReads,
    user_id: UUID = Depends(current_user_id),
) -> UserOut:
    """Get current user information (a read-only route: declared operation)."""
    user = await reads.one(user_by_id, id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return user  # type: ignore[no-any-return]


@auth_router.patch(
    "/me",
    response_model=UserOut,
)
async def update_current_user(
    user_update: UserUpdate,
    db: DbSession,
    user_id: UUID = Depends(current_user_id),
) -> UserOut:
    """Update current user information."""
    updated = await UserService.update_user(
        user_id=user_id,
        user_data=user_update,
        db=db,
        actor=EventActor(user_id=user_id),
        allow_is_active_change=False,
    )
    return UserOut.model_validate(updated)


# === Health Check ===


@auth_router.get(
    "/health",
    dependencies=[
        public(reason="module liveness probe (no caller)"),
    ],
)
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "module": "identity"}
