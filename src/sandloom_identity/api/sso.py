"""SSO API endpoints.

Flow binding: ``/redirect`` (login) and ``/link/redirect`` (authenticated link)
return a ``binding`` secret and set it as an HttpOnly cookie. The callback /
link request must carry it back — via that cookie or the ``X-SSO-Binding``
header — so a flow can only be completed by the client that started it.
"""

from fastapi import APIRouter, Cookie, Depends, Header, Query, Response, status

from sandloom.sdk.infra import CoreRuntime, get_core, settings
from sandloom.sdk.web import DbSession, public, rate_limiter
from sandloom_identity.access import get_current_principal
from sandloom_identity.events import EventActor
from sandloom_identity.schemas.auth import TokenPair
from sandloom_identity.schemas.sso import (
    ExternalIdentityOut,
    MFAChallengeOut,
    MFACompleteIn,
    SSOInitOut,
    SSOLinkIn,
    SSOProviderOut,
    SSOReauthIn,
)
from sandloom_identity.schemas.user import Principal
from sandloom_identity.services.sso_service import PURPOSE_LINK, SSOService

sso_router = APIRouter(prefix="/auth/sso", tags=["sso"], dependencies=[])

_COOKIE = SSOService.BINDING_COOKIE


def _set_binding_cookie(response: Response, binding: str | None) -> None:
    if not binding:
        return
    response.set_cookie(
        _COOKIE,
        binding,
        max_age=SSOService.STATE_TTL_SECONDS,
        httponly=True,
        secure=bool(settings.is_prod),
        samesite="lax",
        path="/auth/sso",
    )


def _binding(cookie_value: str | None, header_value: str | None) -> str | None:
    return header_value or cookie_value


@sso_router.get(
    "/providers",
    response_model=list[SSOProviderOut],
    dependencies=[
        public(reason="login page: list the SSO providers"),
    ],
)
async def get_sso_providers() -> list[SSOProviderOut]:
    """Get list of available SSO providers."""
    return await SSOService.get_providers()


@sso_router.get(
    "/{provider}/redirect",
    response_model=SSOInitOut,
    dependencies=[
        public(reason="SSO login starts before the caller has a token"),
    ],
)
async def sso_redirect(
    provider: str,
    response: Response,
    redirect_uri: str = Query(None, description="Custom redirect URI"),
    _: None = Depends(rate_limiter(requests=10, period=60)),
) -> SSOInitOut:
    """Start an SSO login: provider URL + state bound to this client.

    ``redirect_uri`` is validated against the ``SSO_ALLOWED_REDIRECT_URIS``
    allowlist inside the service (open-redirect protection) — a value not on the
    list is rejected with 400.
    """
    out = await SSOService.get_redirect_url(provider, redirect_uri)
    _set_binding_cookie(response, out.binding)
    return out


@sso_router.get(
    "/{provider}/link/redirect",
    response_model=SSOInitOut,
)
async def sso_link_redirect(
    provider: str,
    response: Response,
    redirect_uri: str = Query(None, description="Custom redirect URI"),
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=10, period=60)),
) -> SSOInitOut:
    """Start linking a provider to the CURRENT account (state bound to it)."""
    out = await SSOService.get_redirect_url(
        provider, redirect_uri, purpose=PURPOSE_LINK, user_id=principal.id
    )
    _set_binding_cookie(response, out.binding)
    return out


@sso_router.get(
    "/{provider}/callback",
    response_model=TokenPair | MFAChallengeOut,
    dependencies=[
        public(
            reason="SSO callback: provider assertion + binding cookie",
            issues_tokens=True,
        ),
    ],
)
async def sso_callback(
    provider: str,
    response: Response,
    db: DbSession,
    core: CoreRuntime = Depends(get_core),
    code: str = Query(..., description="Authorization code from provider"),
    state: str = Query(..., description="State parameter for CSRF protection"),
    binding_cookie: str | None = Cookie(None, alias=_COOKIE),
    binding_header: str | None = Header(None, alias=SSOService.BINDING_HEADER),
    _: None = Depends(rate_limiter(requests=20, period=60)),
) -> TokenPair | MFAChallengeOut:
    """Handle the SSO callback.

    Returns tokens, or ``{"mfa_required": true, "mfa_token": …}`` when the
    account has 2FA — then POST ``/auth/sso/mfa`` with the token and a code.
    """
    result = await SSOService.handle_callback(
        provider,
        code,
        state,
        db,
        EventActor(),
        core,
        binding=_binding(binding_cookie, binding_header),
    )
    response.delete_cookie(_COOKIE, path="/auth/sso")
    return result


@sso_router.post(
    "/mfa",
    response_model=TokenPair,
    dependencies=[
        public(
            reason="SSO MFA step: the MFA challenge token is the credential",
            issues_tokens=True,
        ),
    ],
)
async def sso_complete_mfa(
    data: MFACompleteIn,
    db: DbSession,
    core: CoreRuntime = Depends(get_core),
    _: None = Depends(rate_limiter(requests=10, period=60, fail_closed=True)),
) -> TokenPair:
    """Complete an SSO login with the account's second factor."""
    return await SSOService.complete_mfa(data.mfa_token, data.otp, db, None, core)


@sso_router.post(
    "/{provider}/link",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def link_sso_provider(
    provider: str,
    data: SSOLinkIn,
    response: Response,
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
    binding_cookie: str | None = Cookie(None, alias=_COOKIE),
    binding_header: str | None = Header(None, alias=SSOService.BINDING_HEADER),
    _: None = Depends(rate_limiter(requests=10, period=60, fail_closed=True)),
) -> None:
    """Link an SSO provider to the current account (state from /link/redirect)."""
    await SSOService.link_provider(
        principal.id,
        provider,
        data.code,
        data.state,
        db,
        EventActor(user_id=principal.id),
        binding=_binding(binding_cookie, binding_header),
        current_password=data.current_password,
        token_claims=principal.token_claims,
    )
    response.delete_cookie(_COOKIE, path="/auth/sso")
    return None


@sso_router.delete(
    "/{provider}/unlink",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_sso_provider(
    provider: str,
    db: DbSession,
    data: SSOReauthIn | None = None,
    principal: Principal = Depends(get_current_principal),
    _: None = Depends(rate_limiter(requests=10, period=60, fail_closed=True)),
) -> None:
    """Unlink SSO provider from current user account."""
    await SSOService.unlink_provider(
        principal.id,
        provider,
        db,
        EventActor(user_id=principal.id),
        current_password=data.current_password if data else None,
        token_claims=principal.token_claims,
    )
    return None


@sso_router.get(
    "/me/linked",
    response_model=list[ExternalIdentityOut],
)
async def get_my_linked_providers(
    db: DbSession,
    principal: Principal = Depends(get_current_principal),
) -> list[ExternalIdentityOut]:
    """Get list of SSO providers linked to current user."""
    return await SSOService.get_user_linked_providers(principal.id, db)
