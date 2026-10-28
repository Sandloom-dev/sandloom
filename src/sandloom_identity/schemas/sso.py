"""SSO schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sandloom_identity.providers import Provider


class SSOProviderOut(BaseModel):
    """SSO provider output."""

    code: str
    name: str
    enabled: bool = True


class SSOInitOut(BaseModel):
    """SSO initialization output.

    ``binding`` ties the flow to the client that started it (login-CSRF
    protection). It is also set as an HttpOnly cookie; an API/SPA client that
    cannot rely on the cookie must echo it back in the ``X-SSO-Binding`` header
    on the callback/link request (a cross-site link cannot set that header).
    """

    auth_url: str
    state: str
    binding: str | None = None


class SSOCallbackIn(BaseModel):
    """SSO callback input."""

    code: str
    state: str


class SSOReauthIn(BaseModel):
    """Fresh proof of identity before changing a persistent login method."""

    current_password: str | None = Field(None, max_length=100)


class SSOLinkIn(SSOCallbackIn, SSOReauthIn):
    """OAuth response together with step-up proof for the local account."""


class MFAChallengeOut(BaseModel):
    """Returned instead of tokens when the account requires a second factor."""

    mfa_required: Literal[True] = True
    mfa_token: str
    expires_in: int


class MFACompleteIn(BaseModel):
    """Second-factor completion of an SSO login."""

    mfa_token: str = Field(..., min_length=16, max_length=256)
    otp: str = Field(..., min_length=6, max_length=32)


class ExternalIdentityOut(BaseModel):
    """External identity output."""

    model_config = ConfigDict(from_attributes=True)

    provider: Provider
    external_id: str
    email: str | None
