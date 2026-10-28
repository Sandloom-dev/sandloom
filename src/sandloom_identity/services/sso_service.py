"""SSO service for handling Single Sign-On operations.

Security model
--------------
* **State is bound to the initiating client.** ``/redirect`` returns a random
  ``binding`` secret (also set as an HttpOnly cookie); only its hash is stored
  with the state. The callback must present the same binding (cookie or
  ``X-SSO-Binding`` header — neither can be planted by a cross-site link), so an
  attacker cannot make a victim's browser complete the attacker's login (login
  CSRF). Link flows are additionally bound to the initiating ``user_id``.
* **State is single-use** via an atomic ``add`` claim (``SET NX``), and carries
  the provider, purpose, validated ``redirect_uri`` (re-used verbatim for the
  code exchange), the PKCE ``code_verifier`` and the OIDC ``nonce``.
* **Account linking by email requires two verifications.** An SSO identity is
  auto-linked to an existing local account only when the provider asserts the
  email is verified AND the local account's email is verified. Otherwise the
  login is refused with instructions to sign in and link explicitly through the
  authenticated ``/link`` flow. A new local account is created only from a
  provider-verified email. (Unverified password registrations cannot sign in at
  all, so a pre-registered victim email is inert until the mailbox owner
  verifies it or resets the password.)
* **SSO honours the account's second factor and ``is_active``** in every branch:
  a 2FA-enabled account gets an ``MFAChallengeOut`` instead of tokens and must
  finish with :meth:`SSOService.complete_mfa`. An email auto-link to such an
  account is carried in the challenge and only written (row + audit +
  ``identity.sso_linked.v1``) after the second factor succeeds.
* **A mis-bound callback cannot burn the state.** The binding, provider and
  purpose are checked against the stored state BEFORE the one-time claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.infra import cache, settings
from sandloom.sdk.security import ensure_user_not_blocked
from sandloom_identity import lockout
from sandloom_identity.events import (
    EventActor,
    SSOLinkedEvent,
    SSOUnlinkedEvent,
    create_user_logged_in_event,
    create_user_registered_event,
)
from sandloom_identity.integrations import (
    OAuthClient,
    OAuthClientFactory,
    email_sender,
    make_pkce_pair,
)
from sandloom_identity.models.external_identity import ExternalIdentity
from sandloom_identity.models.user import User
from sandloom_identity.providers import Provider
from sandloom_identity.schemas.auth import TokenPair
from sandloom_identity.schemas.sso import (
    ExternalIdentityOut,
    MFAChallengeOut,
    SSOInitOut,
    SSOProviderOut,
)
from sandloom_identity.services.auth_service import _email_after_commit
from sandloom_identity.services.reauth import require_reauth
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.services.twofa_service import TwoFAService

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.sdk.infra import CoreRuntime

logger = logging.getLogger("sandloom_identity.sso")

PURPOSE_LOGIN = "login"
PURPOSE_LINK = "link"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _bad_state() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired state parameter. This may indicate a CSRF attack.",
    )


class SSOService:
    """Service for Single Sign-On operations."""

    STATE_TTL_SECONDS = 600  # 10 minutes
    STATE_PREFIX = "sso_state:"
    MFA_TTL_SECONDS = 300  # 5 minutes
    MFA_PREFIX = "identity:sso_mfa:"
    BINDING_COOKIE = "sandloom_sso_binding"
    BINDING_HEADER = "X-SSO-Binding"

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _validate_redirect_uri(redirect_uri: str | None) -> None:
        """Validate redirect URI against allowed list for security."""
        if not redirect_uri:
            return  # No custom redirect, will use default

        allowed_uris = [
            uri.strip()
            for uri in settings.sso_allowed_redirect_uris.split(",")
            if uri.strip()
        ]

        if redirect_uri not in allowed_uris:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Invalid redirect_uri. Must be one of: " + ", ".join(allowed_uris)
                ),
            )

    @staticmethod
    def _provider(provider: str) -> Provider:
        try:
            return Provider(provider.lower())
        except (ValueError, AttributeError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported provider: {provider}",
            ) from e

    @staticmethod
    def _client(provider_enum: Provider, provider: str) -> OAuthClient:
        oauth_client = OAuthClientFactory.create_client(provider_enum)
        if not oauth_client:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider {provider} is not configured",
            )
        if not getattr(oauth_client, "implemented", True):
            # Configured but its login flow is unfinished (e.g. Telegram):
            # refuse up front instead of failing at the callback exchange.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider {provider} is not supported yet",
            )
        return oauth_client

    @staticmethod
    async def _store_state(state: str, payload: dict[str, Any]) -> None:
        """Store SSO state (and its binding/PKCE/nonce context) in the cache."""
        key = f"{SSOService.STATE_PREFIX}{state}"
        await cache().set(
            key, json.dumps(payload), ttl_seconds=SSOService.STATE_TTL_SECONDS
        )

    @staticmethod
    async def _validate_and_consume_state(
        state: str,
        *,
        provider: Provider,
        binding: str | None,
        purpose: str,
        user_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Validate SSO state, consume it atomically, and return its context."""
        if not state or not binding:
            raise _bad_state()
        provider_cache = cache()
        key = f"{SSOService.STATE_PREFIX}{state}"

        raw = await provider_cache.get(key)
        if not raw:
            raise _bad_state()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise _bad_state() from exc
        if not isinstance(payload, dict):
            raise _bad_state()

        # Check the binding / provider / purpose BEFORE consuming: a presenter
        # without the initiator's binding (e.g. a victim's browser following an
        # attacker's link, or a guessed state) must not be able to burn the
        # legitimate flow's single-use state.
        expected_hash = payload.get("binding_hash")
        if (
            not isinstance(expected_hash, str)
            or not hmac.compare_digest(expected_hash, _sha256(binding))
            or payload.get("provider") != provider.value
            or payload.get("purpose") != purpose
        ):
            raise _bad_state()
        if purpose == PURPOSE_LINK and payload.get("user_id") != str(user_id):
            raise _bad_state()

        # Atomic one-time claim: only the first (bound) presenter wins (SET NX).
        # one-time SSO state (security state)
        if not await provider_cache.add(
            f"{key}:used", "1", ttl_seconds=SSOService.STATE_TTL_SECONDS
        ):
            raise _bad_state()
        await provider_cache.delete(key)
        return payload

    @staticmethod
    async def _exchange(
        oauth_client: OAuthClient, provider: str, code: str, ctx: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            user_info = await oauth_client.exchange_code_for_user_info(
                code,
                redirect_uri=ctx.get("redirect_uri"),
                code_verifier=ctx.get("code_verifier"),
                nonce=ctx.get("nonce"),
            )
        except Exception as e:
            # Provider error bodies can carry tokens/PII: log, return generic.
            logger.warning(
                "identity: SSO code exchange with %s failed (%s)",
                provider,
                type(e).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to authenticate with {provider}",
            ) from e
        if not user_info.get("id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to authenticate with {provider}",
            )
        return user_info

    @staticmethod
    async def _find_identity(
        provider_enum: Provider, user_info: dict[str, Any], db: AsyncSession
    ) -> ExternalIdentity | None:
        result = await db.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == provider_enum,
                ExternalIdentity.external_id == str(user_info["id"]),
            )
        )
        return result.scalar_one_or_none()

    # --- public API ---------------------------------------------------------

    @staticmethod
    async def get_providers() -> list[SSOProviderOut]:
        """Get list of available SSO providers."""
        providers: list[SSOProviderOut] = []

        if settings.google_client_id and settings.google_client_secret:
            providers.append(SSOProviderOut(code="google", name="Google", enabled=True))

        if getattr(settings, "microsoft_client_id", None) and getattr(
            settings, "microsoft_client_secret", None
        ):
            providers.append(
                SSOProviderOut(code="microsoft", name="Microsoft", enabled=True)
            )

        if settings.github_client_id and settings.github_client_secret:
            providers.append(SSOProviderOut(code="github", name="GitHub", enabled=True))

        if getattr(settings, "linkedin_client_id", None) and getattr(
            settings, "linkedin_client_secret", None
        ):
            providers.append(
                SSOProviderOut(code="linkedin", name="LinkedIn", enabled=True)
            )

        # Telegram is deliberately NOT advertised even when TELEGRAM_BOT_TOKEN
        # is set: its login-widget exchange is not implemented
        # (TelegramOAuthClient.implemented is False), so offering it would send
        # users into a flow that can only fail.

        return providers

    @staticmethod
    async def get_redirect_url(
        provider: str,
        redirect_uri: str | None = None,
        *,
        purpose: str = PURPOSE_LOGIN,
        user_id: UUID | None = None,
    ) -> SSOInitOut:
        """Start an SSO flow: create bound state and the provider URL.

        The returned ``binding`` must come back on the callback (cookie or
        ``X-SSO-Binding`` header). ``purpose="link"`` requires ``user_id``.
        """
        SSOService._validate_redirect_uri(redirect_uri)
        provider_enum = SSOService._provider(provider)
        oauth_client = SSOService._client(provider_enum, provider)
        if purpose == PURPOSE_LINK and user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        code_verifier: str | None = None
        code_challenge: str | None = None
        if oauth_client.supports_pkce:
            code_verifier, code_challenge = make_pkce_pair()
        nonce = secrets.token_urlsafe(24) if oauth_client.uses_nonce else None

        await SSOService._store_state(
            state,
            {
                "provider": provider_enum.value,
                "purpose": purpose,
                "user_id": str(user_id) if user_id else None,
                "binding_hash": _sha256(binding),
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "nonce": nonce,
            },
        )

        auth_url = oauth_client.get_auth_url(
            state=state,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            nonce=nonce,
        )
        return SSOInitOut(auth_url=auth_url, state=state, binding=binding)

    @staticmethod
    async def handle_callback(
        provider: str,
        code: str,
        state: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        core: CoreRuntime | None = None,
        *,
        binding: str | None = None,
    ) -> TokenPair | MFAChallengeOut:
        """Handle the SSO login callback.

        Returns tokens, or an :class:`MFAChallengeOut` when the account has 2FA.
        """
        provider_enum = SSOService._provider(provider)
        # Validate state first - CRITICAL for CSRF protection
        ctx = await SSOService._validate_and_consume_state(
            state, provider=provider_enum, binding=binding, purpose=PURPOSE_LOGIN
        )
        oauth_client = SSOService._client(provider_enum, provider)
        user_info = await SSOService._exchange(oauth_client, provider, code, ctx)
        actor = actor or EventActor()

        external_identity = await SSOService._find_identity(
            provider_enum, user_info, db
        )

        pending_link: dict[str, Any] | None = None
        if external_identity is not None:
            user_result = await db.execute(
                select(User).where(User.id == external_identity.user_id)
            )
            user = user_result.scalar_one()
            SSOService._ensure_active(user)
        else:
            user, via = await SSOService._resolve_unlinked(provider, user_info, db)
            pending_link = {
                "external_id": str(user_info["id"]),
                "email": user_info.get("email"),
                "via": via,
            }

        if await TwoFAService.is_2fa_enabled(user, db):
            # The auto-link is part of the login and must NOT commit before the
            # second factor succeeds (the challenge response commits the
            # request UoW): carry it in the challenge, apply it in complete_mfa.
            return await SSOService._start_mfa(
                user.id, provider, pending_link=pending_link
            )

        if pending_link is not None:
            await SSOService._link(
                user,
                provider,
                provider_enum,
                pending_link,
                db,
                actor,
                pending_link["via"],
            )
        return await SSOService._finish_login(user, provider, db, actor, core)

    @staticmethod
    def _ensure_active(user: User) -> None:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is deactivated",
            )

    @staticmethod
    async def _resolve_unlinked(
        provider: str,
        user_info: dict[str, Any],
        db: AsyncSession,
    ) -> tuple[User, str]:
        """Find-or-create the local user for a not-yet-linked SSO identity.

        Returns ``(user, via)``. The caller performs the link: immediately, or
        only after the second factor for a 2FA-enabled account.
        """
        email = user_info.get("email")
        email_verified = bool(user_info.get("email_verified")) and bool(email)
        if not email_verified:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{provider} did not provide a verified email address, so it "
                    "cannot be used to sign up or sign in automatically. Sign in "
                    f"to your account and link {provider} from your account "
                    "settings instead."
                ),
            )

        user_result = await db.execute(select(User).where(User.email == email))
        user = user_result.scalar_one_or_none()

        if user is not None:
            SSOService._ensure_active(user)
            if not user.is_verified:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "An account with this email already exists but its email "
                        "address has not been verified. Verify it (or reset your "
                        f"password), sign in, then link {provider} from your "
                        "account settings."
                    ),
                )
            via = "auto_link_verified_email"
        else:
            now = datetime.now(UTC)
            user = User(
                email=email,
                full_name=(
                    str(user_info.get("name") or "").strip()
                    or str(email).split("@", 1)[0]
                )[:100],
                is_active=True,
                is_verified=True,  # the provider verified the address
                email_verified_at=now,
            )
            try:
                async with db.begin_nested():
                    db.add(user)
                    await db.flush()
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Sign-in conflict, please retry",
                ) from exc
            via = "signup"
            event = create_user_registered_event(
                user_id=user.id, actor=EventActor(user_id=user.id)
            )
            await event.publish()

        return user, via

    @staticmethod
    async def _link(
        user: User,
        provider: str,
        provider_enum: Provider,
        link: dict[str, Any],
        db: AsyncSession,
        actor: EventActor,
        via: str,
    ) -> None:
        """Write the ExternalIdentity (``link``: ``external_id`` + ``email``)."""
        external_identity = ExternalIdentity(
            user_id=user.id,
            provider=provider_enum,
            external_id=str(link["external_id"]),
            email=link.get("email"),
        )
        try:
            async with db.begin_nested():
                db.add(external_identity)
                await db.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This {provider} account is already linked to a user",
            ) from exc

        record_audit(
            "identity.sso.linked",
            actor_id=actor.user_id or user.id,
            resource_type="user",
            resource_id=str(user.id),
            data={"provider": provider_enum.value, "via": via},
            required=True,
        )
        event = SSOLinkedEvent(
            actor=actor, payload={"user_id": str(user.id), "provider": provider}
        )
        await event.publish()

    @staticmethod
    async def _finish_login(
        user: User,
        provider: str,
        db: AsyncSession,
        actor: EventActor,
        core: CoreRuntime | None,
    ) -> TokenPair:
        SSOService._ensure_active(user)
        await ensure_user_not_blocked(user.id)  # before any login side effect
        tokens = await TokenService.create_tokens(user, db, core, block_checked=True)
        login_event = create_user_logged_in_event(
            user_id=user.id, method="sso", provider=provider, actor=actor
        )
        await login_event.publish()
        # Last, flushed by the UoW commit: the UPDATE row-locks the user until
        # commit (see AuthService.login).
        user.last_login_at = datetime.now(UTC)
        return tokens

    # --- second factor ------------------------------------------------------

    @staticmethod
    async def _start_mfa(
        user_id: UUID,
        provider: str,
        *,
        pending_link: dict[str, Any] | None = None,
    ) -> MFAChallengeOut:
        mfa_token = secrets.token_urlsafe(32)
        challenge: dict[str, Any] = {"user_id": str(user_id), "provider": provider}
        if pending_link is not None:
            challenge["pending_link"] = pending_link
        await cache().set(
            SSOService.MFA_PREFIX + _sha256(mfa_token),
            json.dumps(challenge),
            ttl_seconds=SSOService.MFA_TTL_SECONDS,
        )
        return MFAChallengeOut(
            mfa_token=mfa_token, expires_in=SSOService.MFA_TTL_SECONDS
        )

    @staticmethod
    async def complete_mfa(
        mfa_token: str,
        otp: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        core: CoreRuntime | None = None,
    ) -> TokenPair:
        """Finish an SSO login that required a second factor."""
        invalid = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired 2FA challenge",
        )
        key = SSOService.MFA_PREFIX + _sha256(mfa_token)
        raw = await cache().get(key)
        if not raw:
            raise invalid
        try:
            challenge = json.loads(raw)
            user_id = UUID(challenge["user_id"])
            provider = str(challenge.get("provider") or "sso")
            pending_link = challenge.get("pending_link")
            if pending_link is not None and (
                not isinstance(pending_link, dict)
                or not pending_link.get("external_id")
            ):
                raise ValueError("malformed pending link")
        except (TypeError, ValueError, KeyError) as exc:
            raise invalid from exc

        subject = lockout.subject_for_user(user_id, "2fa")
        await lockout.ensure_not_locked(subject)

        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise invalid
        SSOService._ensure_active(user)
        # Emergency block: refuse before the challenge is consumed or linked.
        await ensure_user_not_blocked(user.id)

        if not await TwoFAService.verify_2fa_code(user, otp, db):
            if await lockout.register_failure(subject):
                await cache().delete(key)  # locked: burn the challenge too
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA code"
            )
        # One-time: only the first successful completion may mint tokens.
        # one-time 2FA challenge (security state)
        if not await cache().add(
            f"{key}:used", "1", ttl_seconds=SSOService.MFA_TTL_SECONDS
        ):
            raise invalid
        await cache().delete(key)
        await lockout.reset_failures(subject)

        actor = actor or EventActor()
        if pending_link is not None:
            # Deferred auto-link: written only now, after the second factor.
            await SSOService._link(
                user,
                provider,
                SSOService._provider(provider),
                pending_link,
                db,
                actor,
                str(pending_link.get("via") or "auto_link_verified_email"),
            )
        return await SSOService._finish_login(user, provider, db, actor, core)

    # --- linking ------------------------------------------------------------

    @staticmethod
    @sensitive_effect("security", step_up=300)
    async def link_provider(
        user: User | UUID,
        provider: str,
        code: str,
        state: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        *,
        binding: str | None = None,
        current_password: str | None = None,
        token_claims: dict[str, Any] | None = None,
    ) -> None:
        """Link an SSO provider to the authenticated user's account.

        The state must have been created by THIS user's ``/link/redirect`` call
        and be presented with its binding.
        """
        user = await SSOService._load_user(user, db)
        await require_reauth(user, current_password, token_claims=token_claims)
        provider_enum = SSOService._provider(provider)
        ctx = await SSOService._validate_and_consume_state(
            state,
            provider=provider_enum,
            binding=binding,
            purpose=PURPOSE_LINK,
            user_id=user.id,
        )
        oauth_client = SSOService._client(provider_enum, provider)
        user_info = await SSOService._exchange(oauth_client, provider, code, ctx)

        existing = await SSOService._find_identity(provider_enum, user_info, db)
        if existing and existing.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This {provider} account is already linked to another user",
            )
        if existing and existing.user_id == user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This {provider} account is already linked to your account",
            )

        await SSOService._link(
            user,
            provider,
            provider_enum,
            {"external_id": str(user_info["id"]), "email": user_info.get("email")},
            db,
            actor or EventActor(user_id=user.id),
            "explicit_link",
        )
        email = user.email
        await _email_after_commit(
            lambda: email_sender.send_security_notice(
                email,
                f"A {provider} sign-in method was linked to your account. "
                "If this was not you, contact your platform administrator.",
            )
        )

    @staticmethod
    async def _load_user(user: User | UUID, db: AsyncSession) -> User:
        # Serialize changes to the set of login methods. Two unlinks must not
        # each observe "two methods" and remove a different final identity.
        user_id = user.id if isinstance(user, User) else user
        result = await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        found = result.scalar_one_or_none()
        if found is None or not found.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
            )
        return found

    @staticmethod
    @sensitive_effect("security", step_up=300)
    async def unlink_provider(
        user: User | UUID,
        provider: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        *,
        current_password: str | None = None,
        token_claims: dict[str, Any] | None = None,
    ) -> None:
        """Unlink SSO provider from user account."""
        user = await SSOService._load_user(user, db)
        await require_reauth(user, current_password, token_claims=token_claims)
        provider_enum = SSOService._provider(provider)

        result = await db.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.user_id == user.id,
                ExternalIdentity.provider == provider_enum,
            )
        )
        external_identity = result.scalar_one_or_none()

        if not external_identity:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No {provider} account linked to this user",
            )

        user_external_identities = await db.execute(
            select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
        )
        external_count = len(user_external_identities.scalars().all())

        if external_count == 1 and not user.hashed_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cannot unlink last authentication method. Set a password first."
                ),
            )

        await db.delete(external_identity)
        await db.flush()

        actor = actor or EventActor(user_id=user.id)
        record_audit(
            "identity.sso.unlinked",
            actor_id=actor.user_id or user.id,
            resource_type="user",
            resource_id=str(user.id),
            data={"provider": provider_enum.value},
            required=True,
        )
        event = SSOUnlinkedEvent(
            actor=actor, payload={"user_id": str(user.id), "provider": provider}
        )
        await event.publish()

    @staticmethod
    async def get_user_linked_providers(
        user: User | UUID, db: AsyncSession
    ) -> list[ExternalIdentityOut]:
        """Get list of SSO providers linked to user."""
        user_id = user.id if isinstance(user, User) else user
        result = await db.execute(
            select(ExternalIdentity).where(ExternalIdentity.user_id == user_id)
        )
        external_identities = result.scalars().all()

        return [ExternalIdentityOut.model_validate(ei) for ei in external_identities]
