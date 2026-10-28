"""Authentication schemas."""

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from sandloom_identity.password_policy import password_policy_violation


def _check_password(value: str) -> str:
    violation = password_policy_violation(value)
    if violation:
        raise ValueError(violation)
    return value


class RegisterIn(BaseModel):
    """User registration input."""

    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=100)

    @model_validator(mode="after")
    def check_password_policy(self) -> "RegisterIn":
        violation = password_policy_violation(self.password, str(self.email))
        if violation:
            raise ValueError(violation)
        return self


class RegistrationAcceptedOut(BaseModel):
    """Uniform registration response.

    Identical whether or not the email already had an account, so the endpoint
    cannot be used to enumerate registered addresses. The address owner gets an
    email either way (a verification link, or a "you already have an account"
    notice).
    """

    message: str = (
        "Registration received. Check your email to verify your address and "
        "finish setting up your account."
    )


class LoginIn(BaseModel):
    """User login input."""

    email: EmailStr
    password: str
    otp: str | None = Field(None, description="One-time password for 2FA")


class TokenPair(BaseModel):
    """Token pair response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int


class RefreshTokenIn(BaseModel):
    """Refresh token input."""

    refresh_token: str


class PasswordResetRequestIn(BaseModel):
    """Password reset request input."""

    email: EmailStr


class EmailVerificationResendIn(BaseModel):
    """Request a fresh email-verification link."""

    email: EmailStr


class PasswordResetIn(BaseModel):
    """Password reset input."""

    token: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        return _check_password(value)


class PasswordResetConfirmIn(BaseModel):
    """Password reset confirmation input."""

    token: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        return _check_password(value)


class PasswordChangeIn(BaseModel):
    """Password change input (authenticated; requires the current password)."""

    current_password: str = Field(..., min_length=1, max_length=100)
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        return _check_password(value)


class EmailVerificationIn(BaseModel):
    """Email verification input."""

    token: str
