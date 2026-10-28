"""Two-factor authentication API endpoints.

Changing the second factor requires step-up re-authentication (the current
password, or a sign-in from the last few minutes for password-less accounts) —
see :mod:`sandloom_identity.services.reauth`.
"""

from fastapi import APIRouter, Depends

from sandloom.sdk.web import DbSession, rate_limiter
from sandloom_identity import lockout
from sandloom_identity.access import get_current_principal
from sandloom_identity.events import EventActor
from sandloom_identity.schemas.twofa import (
    TwoFABackupCodeRegenerateIn,
    TwoFADisableIn,
    TwoFAEnableIn,
    TwoFAInitResponse,
    TwoFAReauthIn,
    TwoFAStatusOut,
    TwoFAVerifyIn,
)
from sandloom_identity.schemas.user import Principal
from sandloom_identity.services.twofa_service import TwoFAService

twofa_router = APIRouter(prefix="/auth/2fa", tags=["2fa"], dependencies=[])


@twofa_router.get(
    "/status",
    response_model=TwoFAStatusOut,
)
async def get_2fa_status(
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
) -> TwoFAStatusOut:
    """Get 2FA status for current user."""
    return await TwoFAService.get_2fa_status(principal.id, db)


@twofa_router.post(
    "/init",
    response_model=TwoFAInitResponse,
)
async def init_2fa(
    db: DbSession,
    data: TwoFAReauthIn | None = None,
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=10, period=60, fail_closed=True)),
) -> TwoFAInitResponse:
    """Initialize 2FA setup for current user (requires re-auth)."""
    return await TwoFAService.init_2fa(
        principal.id,
        db,
        current_password=data.current_password if data else None,
        token_claims=principal.token_claims,
    )


@twofa_router.post(
    "/enable",
    response_model=TwoFAStatusOut,
)
async def enable_2fa_endpoint(
    data: TwoFAEnableIn,
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=10, period=60, fail_closed=True)),
) -> TwoFAStatusOut:
    """Enable 2FA for current user (requires re-auth + OTP)."""
    return await TwoFAService.enable_2fa(
        principal.id,
        data.otp,
        db,
        EventActor(user_id=principal.id),
        current_password=data.current_password,
        token_claims=principal.token_claims,
    )


@twofa_router.post(
    "/disable",
    response_model=TwoFAStatusOut,
)
async def disable_2fa_endpoint(
    data: TwoFADisableIn,
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=10, period=60, fail_closed=True)),
) -> TwoFAStatusOut:
    """Disable 2FA for current user (requires re-auth + OTP)."""
    return await TwoFAService.disable_2fa(
        principal.id,
        data.otp,
        db,
        EventActor(user_id=principal.id),
        current_password=data.current_password,
        token_claims=principal.token_claims,
    )


@twofa_router.post(
    "/verify",
    response_model=dict[str, bool],
)
async def verify_2fa_code(
    data: TwoFAVerifyIn,
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=30, period=60, fail_closed=True)),
) -> dict[str, bool]:
    """Verify (and consume) a 2FA code for current user.

    Per-user lockout applies, so this endpoint cannot be used to brute-force
    the second factor.
    """
    subject = lockout.subject_for_user(principal.id, "2fa")
    await lockout.ensure_not_locked(subject)
    is_valid = await TwoFAService.verify_2fa_code(principal.id, data.otp, db)
    if is_valid:
        await lockout.reset_failures(subject)
    else:
        await lockout.register_failure(subject)
    return {"valid": is_valid}


@twofa_router.get(
    "/backup-codes",
    response_model=dict[str, list[str]],
)
async def get_backup_codes(
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, list[str]]:
    """Get placeholders for the remaining backup codes (codes are hashed)."""
    codes = await TwoFAService.get_existing_backup_codes(principal.id, db)
    return {"backup_codes": codes}


@twofa_router.post(
    "/backup-codes/regenerate",
    response_model=dict[str, list[str]],
)
async def regenerate_backup_codes(
    data: TwoFABackupCodeRegenerateIn,
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=5, period=3600)),
) -> dict[str, list[str]]:
    """Regenerate backup codes (requires 2FA enabled, re-auth and an OTP)."""
    new_codes = await TwoFAService.regenerate_backup_codes(
        principal.id,
        db,
        EventActor(user_id=principal.id),
        otp=data.otp,
        current_password=data.current_password,
        token_claims=principal.token_claims,
    )
    return {"backup_codes": new_codes}
