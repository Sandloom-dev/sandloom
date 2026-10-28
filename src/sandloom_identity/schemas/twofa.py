"""Two-factor authentication schemas."""

from pydantic import BaseModel, Field


class TwoFAInitResponse(BaseModel):
    """2FA initialization response."""

    secret: str
    qr_code: str
    backup_codes: list[str]


class TwoFASetupOut(BaseModel):
    """2FA setup output."""

    secret: str
    qr_code: str
    backup_codes: list[str]


class TwoFAReauthIn(BaseModel):
    """Step-up re-authentication for 2FA setup."""

    current_password: str | None = Field(
        None,
        max_length=100,
        description=(
            "Current password (step-up re-auth). Required for accounts with a "
            "password; SSO-only accounts need a sign-in from the last 5 minutes."
        ),
    )


class TwoFAEnableIn(BaseModel):
    """2FA enable input."""

    otp: str = Field(..., min_length=6, max_length=6)
    current_password: str | None = Field(
        None,
        max_length=100,
        description=(
            "Current password (step-up re-auth). Required for accounts with a "
            "password; SSO-only accounts need a sign-in from the last 5 minutes."
        ),
    )


class TwoFAConfirmIn(BaseModel):
    """2FA confirmation input."""

    code: str = Field(..., min_length=6, max_length=6)


class TwoFACodeIn(BaseModel):
    """2FA code input."""

    code: str = Field(..., min_length=6, max_length=6)


class TwoFAVerifyIn(BaseModel):
    """2FA verification input."""

    otp: str = Field(..., min_length=6, max_length=6)


class TwoFADisableIn(BaseModel):
    """2FA disable input (OTP + step-up re-auth)."""

    otp: str = Field(..., pattern=r"^(?:[0-9]{6}|[a-fA-F0-9]{20})$")
    current_password: str | None = Field(
        None,
        max_length=100,
        description=(
            "Current password (step-up re-auth). Required for accounts with a "
            "password; SSO-only accounts need a sign-in from the last 5 minutes."
        ),
    )


class TwoFABackupCodeIn(BaseModel):
    """2FA backup code input."""

    backup_code: str


class TwoFABackupCodeRegenerateIn(BaseModel):
    """2FA backup code regeneration input - requires OTP + re-auth."""

    otp: str = Field(..., pattern=r"^(?:[0-9]{6}|[a-fA-F0-9]{20})$")
    current_password: str | None = Field(
        None,
        max_length=100,
        description=(
            "Current password (step-up re-auth). Required for accounts with a "
            "password; SSO-only accounts need a sign-in from the last 5 minutes."
        ),
    )


class TwoFAStatusOut(BaseModel):
    """2FA status output."""

    enabled: bool
    has_backup_codes: bool
