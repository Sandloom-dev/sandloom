"""Identity module integrations for SSO and email.

OAuth clients normalise every provider's profile into one dict::

    {"id": str, "email": str | None, "email_verified": bool, "name": str | None}

``email_verified`` is True ONLY when the provider asserts that it verified the
address. The SSO service never links/creates a local account by email unless
this flag is set (see ``SSOService.handle_callback``), so a client must never
default it to True.

Outbound mail goes through the framework notifier (``sandloom.core.di.notifier``) —
identity imports no mail SDK, and secret links travel only in the notification
body (the default ``log`` driver records channel/recipient/subject, never the
body).
"""

import abc
import base64
import hashlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from sandloom.sdk.infra import Notification, notifier, settings
from sandloom_identity.providers import Provider

logger = logging.getLogger("sandloom_identity.integrations")

# Bound every outbound SSO call so a hung IdP token endpoint cannot pin a
# request worker indefinitely (connect 5s, total 10s).
_SSO_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE ``S256`` (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _truthy(value: Any) -> bool:
    """Provider booleans sometimes arrive as strings ("true")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _decode_jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT signature verification.

    Only for an ``id_token`` received directly from the provider's token
    endpoint over TLS in a confidential-client code exchange — OIDC Core
    §3.1.3.7 allows TLS server validation in place of signature checking there.
    The caller still validates ``aud`` / ``nonce``.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise ValueError("Malformed id_token") from exc
    if not isinstance(claims, dict):
        raise ValueError("Malformed id_token")
    return claims


# --- Email -------------------------------------------------------------------


class EmailSender(abc.ABC):
    """Abstract email sender interface."""

    @abc.abstractmethod
    async def send_verification_email(
        self,
        to_email: str,
        full_name: str | None,
        verification_url: str,
    ) -> None:
        """Send email verification email."""

    @abc.abstractmethod
    async def send_password_reset_email(
        self,
        to_email: str,
        full_name: str | None,
        reset_url: str,
    ) -> None:
        """Send password reset email."""

    async def send_registration_attempt_email(
        self,
        to_email: str,
        full_name: str | None,
        sign_in_url: str,
        reset_url: str,
    ) -> None:
        """Tell an existing account holder someone tried to register again.

        Optional for custom senders (default: send nothing).
        """
        return None

    async def send_security_notice(self, to_email: str, message: str) -> None:
        """Optional security notices for custom mail adapters."""
        return None


class NotifierEmailSender(EmailSender):
    """Email sender backed by the framework notifier capability.

    Delivery failures are logged (without the link/token) and swallowed: the
    caller's response must not differ by whether a mail went out (account
    enumeration), and a reset link must never land in a log line.
    """

    async def _send(
        self, to_email: str, subject: str, body: str, kind: str, **fields: str | None
    ) -> None:
        try:
            await notifier().send(
                Notification(
                    channel="email",
                    recipient=to_email,
                    subject=subject,
                    body=body,
                    data={"kind": kind},
                    body_fields={k: v for k, v in fields.items() if v is not None},
                )
            )
        except Exception as exc:  # noqa: BLE001 - email failure never fails the flow
            logger.warning(
                "identity: %s email delivery failed (%s)", kind, type(exc).__name__
            )

    async def send_verification_email(
        self,
        to_email: str,
        full_name: str | None,
        verification_url: str,
    ) -> None:
        """Send the email-verification link."""
        greeting = f"Hi {full_name}," if full_name else "Hi,"
        body = (
            f"{greeting}\n\nPlease confirm your email address by opening the link "
            f"below (valid for 24 hours):\n\n{verification_url}\n\n"
            "If you did not create an account, you can ignore this email."
        )
        await self._send(
            to_email,
            "Verify your email address",
            body,
            "email_verification",
            full_name=full_name,
            verification_url=verification_url,
        )

    async def send_security_notice(self, to_email: str, message: str) -> None:
        await self._send(
            to_email,
            "Account security notice",
            message,
            "security_notice",
            message=message,
        )

    async def send_password_reset_email(
        self,
        to_email: str,
        full_name: str | None,
        reset_url: str,
    ) -> None:
        """Send the password-reset link."""
        greeting = f"Hi {full_name}," if full_name else "Hi,"
        body = (
            f"{greeting}\n\nUse the link below to choose a new password (valid for "
            f"1 hour, single use):\n\n{reset_url}\n\n"
            "If you did not request a password reset, you can ignore this email."
        )
        await self._send(
            to_email,
            "Reset your password",
            body,
            "password_reset",
            full_name=full_name,
            reset_url=reset_url,
        )

    async def send_registration_attempt_email(
        self,
        to_email: str,
        full_name: str | None,
        sign_in_url: str,
        reset_url: str,
    ) -> None:
        """Notify the owner of an existing account about a sign-up attempt."""
        greeting = f"Hi {full_name}," if full_name else "Hi,"
        body = (
            f"{greeting}\n\nSomeone tried to create a new account with this email "
            "address, but you already have one.\n\n"
            f"Sign in: {sign_in_url}\nForgot your password? {reset_url}\n\n"
            "If this was not you, no action is needed."
        )
        await self._send(
            to_email,
            "You already have an account",
            body,
            "registration_attempt",
            full_name=full_name,
            sign_in_url=sign_in_url,
            reset_url=reset_url,
        )


# --- OAuth -------------------------------------------------------------------


class OAuthClient(abc.ABC):
    """Abstract OAuth client interface."""

    #: Whether the provider accepts PKCE (S256) on its authorization endpoint.
    supports_pkce: bool = False
    #: Whether the client validates an OIDC ``nonce`` from an id_token.
    uses_nonce: bool = False
    #: Whether the login flow is fully implemented. An unimplemented client is
    #: never advertised in `/auth/sso/providers` and its redirect/callback are
    #: rejected, so a configured-but-unfinished provider cannot strand a user.
    implemented: bool = True

    @abc.abstractmethod
    def get_auth_url(
        self,
        state: str,
        redirect_uri: str | None = None,
        *,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Get OAuth authorization URL."""

    @abc.abstractmethod
    async def exchange_code_for_user_info(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for normalised user info."""


def _with_pkce(params: dict[str, str], code_challenge: str | None) -> dict[str, str]:
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return params


class GoogleOAuthClient(OAuthClient):
    """Google OAuth client implementation."""

    supports_pkce = True

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        """Initialize Google OAuth client."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_endpoint = "https://oauth2.googleapis.com/token"  # noqa: S105 - public OAuth endpoint URL
        self.userinfo_endpoint = "https://www.googleapis.com/oauth2/v2/userinfo"

    def get_auth_url(
        self,
        state: str,
        redirect_uri: str | None = None,
        *,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Get Google OAuth authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "scope": "openid email profile",
            "response_type": "code",
            "state": state,
        }
        query = urlencode(_with_pkce(params, code_challenge))
        return f"{self.authorization_endpoint}?{query}"

    async def exchange_code_for_user_info(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for tokens and user info."""
        async with httpx.AsyncClient(timeout=_SSO_HTTP_TIMEOUT) as client:
            token_data = {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri or self.redirect_uri,
                "grant_type": "authorization_code",
            }
            if code_verifier:
                token_data["code_verifier"] = code_verifier

            token_response = await client.post(self.token_endpoint, data=token_data)
            token_response.raise_for_status()
            tokens = token_response.json()

            userinfo_response = await client.get(
                self.userinfo_endpoint,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            userinfo_response.raise_for_status()
            user_info = userinfo_response.json()

            # v2 userinfo uses ``verified_email``; OIDC userinfo ``email_verified``.
            verified = _truthy(user_info.get("verified_email")) or _truthy(
                user_info.get("email_verified")
            )
            return {
                "id": str(user_info.get("id") or user_info["sub"]),
                "email": user_info.get("email"),
                "email_verified": verified,
                "name": user_info.get("name"),
            }


class GitHubOAuthClient(OAuthClient):
    """GitHub OAuth client implementation."""

    supports_pkce = True

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        """Initialize GitHub OAuth client."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.authorization_endpoint = "https://github.com/login/oauth/authorize"
        self.token_endpoint = "https://github.com/login/oauth/access_token"  # noqa: S105 - public OAuth endpoint URL
        self.userinfo_endpoint = "https://api.github.com/user"
        self.emails_endpoint = "https://api.github.com/user/emails"

    def get_auth_url(
        self,
        state: str,
        redirect_uri: str | None = None,
        *,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Get GitHub OAuth authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "scope": "user:email",
            "state": state,
        }
        query = urlencode(_with_pkce(params, code_challenge))
        return f"{self.authorization_endpoint}?{query}"

    async def exchange_code_for_user_info(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for tokens and user info.

        Only the account's PRIMARY *and* VERIFIED address is used; the public
        profile email is never trusted. Without one, ``email`` is ``None``.
        """
        async with httpx.AsyncClient(timeout=_SSO_HTTP_TIMEOUT) as client:
            token_data = {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri or self.redirect_uri,
            }
            if code_verifier:
                token_data["code_verifier"] = code_verifier

            token_response = await client.post(
                self.token_endpoint,
                data=token_data,
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            tokens = token_response.json()
            if "access_token" not in tokens:
                raise ValueError("GitHub token exchange returned no access_token")

            auth_headers = {
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json",
            }
            userinfo_response = await client.get(
                self.userinfo_endpoint, headers=auth_headers
            )
            userinfo_response.raise_for_status()
            user_info = userinfo_response.json()

            emails_response = await client.get(
                self.emails_endpoint, headers=auth_headers
            )
            emails_response.raise_for_status()
            emails = emails_response.json()

            verified_primary: str | None = None
            for entry in emails if isinstance(emails, list) else []:
                if entry.get("primary") and entry.get("verified") is True:
                    verified_primary = entry.get("email")
                    break

            return {
                "id": str(user_info["id"]),
                "email": verified_primary,
                "email_verified": verified_primary is not None,
                "name": user_info.get("name"),
            }


class MicrosoftOAuthClient(OAuthClient):
    """Microsoft identity platform (OIDC) client.

    The ``/common`` endpoint admits accounts from ANY tenant, where ``mail`` /
    ``userPrincipalName`` are attacker-controllable, so neither is trusted for
    account linking. Identity is the immutable ``tid:oid`` pair from the
    id_token; the email counts as verified only with the ``xms_edov`` (email
    domain owner verified) optional claim configured on the app registration.
    Without it, Microsoft sign-in works for accounts already linked via the
    authenticated ``/link`` flow, but never auto-links or creates by email.
    """

    supports_pkce = True
    uses_nonce = True

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        """Initialize Microsoft OAuth client."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.authorization_endpoint = (
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        )
        self.token_endpoint = (
            "https://login.microsoftonline.com/common/oauth2/v2.0/token"  # noqa: S105 - public OAuth endpoint URL
        )

    def get_auth_url(
        self,
        state: str,
        redirect_uri: str | None = None,
        *,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Get Microsoft OAuth authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "scope": "openid email profile",
            "response_type": "code",
            "state": state,
        }
        if nonce:
            params["nonce"] = nonce
        query = urlencode(_with_pkce(params, code_challenge))
        return f"{self.authorization_endpoint}?{query}"

    async def exchange_code_for_user_info(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Exchange the code and read identity from the id_token."""
        async with httpx.AsyncClient(timeout=_SSO_HTTP_TIMEOUT) as client:
            token_data = {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri or self.redirect_uri,
                "grant_type": "authorization_code",
            }
            if code_verifier:
                token_data["code_verifier"] = code_verifier

            token_response = await client.post(self.token_endpoint, data=token_data)
            token_response.raise_for_status()
            tokens = token_response.json()

        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError("Microsoft token response has no id_token")
        claims = _decode_jwt_claims_unverified(id_token)

        aud = claims.get("aud")
        if aud != self.client_id:
            raise ValueError("id_token audience mismatch")
        if nonce is not None and claims.get("nonce") != nonce:
            raise ValueError("id_token nonce mismatch")
        oid, tid = claims.get("oid"), claims.get("tid")
        if not oid or not tid:
            raise ValueError("id_token lacks oid/tid")

        verified = _truthy(claims.get("xms_edov"))
        email = claims.get("email") if verified else None
        return {
            "id": f"{tid}:{oid}",
            "email": email,
            "email_verified": bool(email) and verified,
            "name": claims.get("name"),
        }


class LinkedInOAuthClient(OAuthClient):
    """LinkedIn "Sign In with LinkedIn using OpenID Connect" client."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        """Initialize LinkedIn OAuth client."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.authorization_endpoint = "https://www.linkedin.com/oauth/v2/authorization"
        self.token_endpoint = "https://www.linkedin.com/oauth/v2/accessToken"  # noqa: S105 - public OAuth endpoint URL
        self.userinfo_endpoint = "https://api.linkedin.com/v2/userinfo"

    def get_auth_url(
        self,
        state: str,
        redirect_uri: str | None = None,
        *,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Get LinkedIn OAuth authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "scope": "openid profile email",
            "response_type": "code",
            "state": state,
        }
        return f"{self.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code_for_user_info(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for user info (OIDC userinfo)."""
        async with httpx.AsyncClient(timeout=_SSO_HTTP_TIMEOUT) as client:
            token_data = {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri or self.redirect_uri,
                "grant_type": "authorization_code",
            }

            token_response = await client.post(
                self.token_endpoint,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_response.raise_for_status()
            tokens = token_response.json()

            userinfo_response = await client.get(
                self.userinfo_endpoint,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            userinfo_response.raise_for_status()
            user_info = userinfo_response.json()

            name = user_info.get("name") or " ".join(
                p
                for p in (user_info.get("given_name"), user_info.get("family_name"))
                if p
            )
            email = user_info.get("email")
            return {
                "id": str(user_info["sub"]),
                "email": email,
                "email_verified": bool(email)
                and _truthy(user_info.get("email_verified")),
                "name": name or None,
            }


class TelegramOAuthClient(OAuthClient):
    """Telegram OAuth client (NOT implemented yet).

    Telegram's Login Widget uses a hash-signed widget payload, not an OAuth
    code exchange; until that flow lands the provider is hidden from
    `/auth/sso/providers` and its redirect is rejected (``implemented=False``)
    even when ``TELEGRAM_BOT_TOKEN`` is set.
    """

    implemented = False

    def __init__(
        self,
        bot_token: str,
        redirect_uri: str,
    ) -> None:
        """Initialize Telegram OAuth client."""
        self.bot_token = bot_token
        self.redirect_uri = redirect_uri

    def get_auth_url(
        self,
        state: str,
        redirect_uri: str | None = None,
        *,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:
        """Get Telegram OAuth authorization URL."""
        redirect_uri = redirect_uri or self.redirect_uri
        params = {
            "bot_id": self.bot_token.split(":")[0],
            "origin": redirect_uri,
            "request_access": "write",
            "return_to": redirect_uri,
        }
        return f"https://oauth.telegram.org/auth?{urlencode(params)}"

    async def exchange_code_for_user_info(
        self,
        code: str,
        *,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for user info."""
        # Telegram Login Widget uses a different flow (hash-signed widget data).
        raise NotImplementedError(
            "Telegram OAuth implementation needs custom widget integration"
        )


class OAuthClientFactory:
    """Factory for creating OAuth clients."""

    @staticmethod
    def create_client(  # noqa: C901
        provider: Provider, redirect_uri: str | None = None
    ) -> OAuthClient | None:
        """Create OAuth client for provider."""
        if provider == Provider.GOOGLE:
            if not (settings.google_client_id and settings.google_client_secret):
                return None
            return GoogleOAuthClient(
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                redirect_uri=redirect_uri
                or f"{settings.frontend_url}/auth/sso/google/callback",
            )
        elif provider == Provider.MICROSOFT:
            if not (settings.microsoft_client_id and settings.microsoft_client_secret):
                return None
            return MicrosoftOAuthClient(
                client_id=settings.microsoft_client_id,
                client_secret=settings.microsoft_client_secret,
                redirect_uri=redirect_uri
                or f"{settings.frontend_url}/auth/sso/microsoft/callback",
            )
        elif provider == Provider.GITHUB:
            if not (settings.github_client_id and settings.github_client_secret):
                return None
            return GitHubOAuthClient(
                client_id=settings.github_client_id,
                client_secret=settings.github_client_secret,
                redirect_uri=redirect_uri
                or f"{settings.frontend_url}/auth/sso/github/callback",
            )
        elif provider == Provider.LINKEDIN:
            if not (settings.linkedin_client_id and settings.linkedin_client_secret):
                return None
            return LinkedInOAuthClient(
                client_id=settings.linkedin_client_id,
                client_secret=settings.linkedin_client_secret,
                redirect_uri=redirect_uri
                or f"{settings.frontend_url}/auth/sso/linkedin/callback",
            )
        elif provider == Provider.TELEGRAM:
            if not settings.telegram_bot_token:
                return None
            return TelegramOAuthClient(
                bot_token=settings.telegram_bot_token,
                redirect_uri=redirect_uri
                or f"{settings.frontend_url}/auth/sso/telegram/callback",
            )
        raise AssertionError(f"Unhandled provider case: {provider}")


# Global instances
email_sender: EmailSender = NotifierEmailSender()
oauth_factory = OAuthClientFactory()
