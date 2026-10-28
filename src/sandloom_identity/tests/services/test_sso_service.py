"""SSOService against a real database + the test Redis cache.

Pins the hardened behaviour: bound + single-use state with PKCE/nonce context,
never auto-linking unless BOTH the provider and the local account verified the
email, 2FA + is_active enforced on every SSO branch, generic provider errors,
and the authenticated link flow bound to the initiating user.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from sandloom_identity.models.external_identity import ExternalIdentity, Provider
from sandloom_identity.models.user import User
from sandloom_identity.schemas.auth import TokenPair
from sandloom_identity.schemas.sso import MFAChallengeOut
from sandloom_identity.services import sso_service as sso_mod
from sandloom_identity.services.sso_service import PURPOSE_LINK, SSOService
from sandloom_identity.services.twofa_service import TwoFAService


class FakeOAuthClient:
    """Records what the service passes; returns a scripted profile."""

    supports_pkce = True
    uses_nonce = False

    def __init__(self, user_info: dict[str, Any] | Exception) -> None:
        self.user_info = user_info
        self.auth_calls: list[dict[str, Any]] = []
        self.exchange_calls: list[dict[str, Any]] = []

    def get_auth_url(
        self, state, redirect_uri=None, *, code_challenge=None, nonce=None
    ):
        self.auth_calls.append(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "nonce": nonce,
            }
        )
        return f"https://idp.example/auth?state={state}"

    async def exchange_code_for_user_info(
        self, code, *, redirect_uri=None, code_verifier=None, nonce=None
    ):
        self.exchange_calls.append(
            {
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "nonce": nonce,
            }
        )
        if isinstance(self.user_info, Exception):
            raise self.user_info
        return dict(self.user_info)


def _google(info: dict[str, Any] | Exception) -> FakeOAuthClient:
    return FakeOAuthClient(info)


@pytest.fixture
def fake_client():
    holder: dict[str, FakeOAuthClient] = {}

    def install(info: dict[str, Any] | Exception) -> FakeOAuthClient:
        holder["client"] = _google(info)
        return holder["client"]

    with patch.object(
        sso_mod.OAuthClientFactory,
        "create_client",
        side_effect=lambda provider, redirect_uri=None: holder.get("client"),
    ):
        yield install


async def _start(provider: str = "google", **kw):
    return await SSOService.get_redirect_url(provider, **kw)


VERIFIED = {
    "id": "g-1",
    "email": "sso@example.com",
    "email_verified": True,
    "name": "S",
}


class TestProviders:
    @pytest.mark.asyncio
    async def test_unsupported_provider(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await SSOService.get_redirect_url("nope")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_not_configured(self) -> None:
        with patch.object(
            sso_mod.OAuthClientFactory, "create_client", return_value=None
        ):
            with pytest.raises(HTTPException) as exc:
                await SSOService.get_redirect_url("github")
        assert "not configured" in exc.value.detail

    @pytest.mark.asyncio
    async def test_invalid_redirect_uri_rejected(self, fake_client) -> None:
        fake_client(VERIFIED)
        with pytest.raises(HTTPException) as exc:
            await SSOService.get_redirect_url("google", "https://evil.example/cb")
        assert exc.value.status_code == 400


class TestState:
    @pytest.mark.asyncio
    async def test_redirect_returns_binding_and_pkce(self, fake_client) -> None:
        client = fake_client(VERIFIED)
        init = await _start()
        assert init.binding and init.state
        assert client.auth_calls[0]["code_challenge"]

    @pytest.mark.asyncio
    async def test_callback_requires_binding(self, test_db, fake_client) -> None:
        fake_client(VERIFIED)
        init = await _start()
        with pytest.raises(HTTPException) as exc:
            await SSOService.handle_callback("google", "c", init.state, test_db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_login_csrf_foreign_binding_rejected(
        self, test_db, fake_client
    ) -> None:
        """A state started by the attacker cannot be completed by a victim."""
        fake_client(VERIFIED)
        attacker = await _start()
        with pytest.raises(HTTPException):
            await SSOService.handle_callback(
                "google", "c", attacker.state, test_db, binding="victim-binding"
            )

    @pytest.mark.asyncio
    async def test_state_single_use(self, test_db, fake_client) -> None:
        fake_client(VERIFIED)
        init = await _start()
        await SSOService.handle_callback(
            "google", "c", init.state, test_db, binding=init.binding
        )
        with pytest.raises(HTTPException):
            await SSOService.handle_callback(
                "google", "c", init.state, test_db, binding=init.binding
            )

    @pytest.mark.asyncio
    async def test_state_bound_to_provider(self, test_db, fake_client) -> None:
        fake_client(VERIFIED)
        init = await _start("google")
        with pytest.raises(HTTPException):
            await SSOService.handle_callback(
                "github", "c", init.state, test_db, binding=init.binding
            )

    @pytest.mark.asyncio
    async def test_exchange_uses_stored_redirect_and_verifier(
        self, test_db, fake_client
    ) -> None:
        from sandloom.sdk.infra import settings

        client = fake_client(VERIFIED)
        allowed = "https://app.example.com/sso/cb"
        with patch.object(settings, "sso_allowed_redirect_uris", allowed):
            init = await _start(redirect_uri=allowed)
        await SSOService.handle_callback(
            "google", "code-1", init.state, test_db, binding=init.binding
        )
        call = client.exchange_calls[0]
        assert call["redirect_uri"] == allowed
        assert call["code_verifier"]  # PKCE verifier from the stored state
        # challenge in the auth URL derives from that verifier
        import base64
        import hashlib

        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(call["code_verifier"].encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        assert client.auth_calls[0]["code_challenge"] == expected

    @pytest.mark.asyncio
    async def test_provider_error_is_generic(self, test_db, fake_client) -> None:
        fake_client(RuntimeError("upstream said: token=abc123 secret"))
        init = await _start()
        with pytest.raises(HTTPException) as exc:
            await SSOService.handle_callback(
                "google", "c", init.state, test_db, binding=init.binding
            )
        assert exc.value.detail == "Failed to authenticate with google"
        assert "abc123" not in exc.value.detail


class TestLinkingPolicy:
    async def _callback(self, test_db, fake_client, info):
        fake_client(info)
        init = await _start()
        return await SSOService.handle_callback(
            "google", "code", init.state, test_db, binding=init.binding
        )

    @pytest.mark.asyncio
    async def test_new_user_created_verified(self, test_db, fake_client) -> None:
        result = await self._callback(test_db, fake_client, VERIFIED)
        assert isinstance(result, TokenPair)
        user = (
            await test_db.execute(select(User).where(User.email == "sso@example.com"))
        ).scalar_one()
        assert user.is_verified is True and user.email_verified_at is not None

    @pytest.mark.asyncio
    async def test_unverified_provider_email_never_links(
        self, test_db, fake_client, make_user
    ) -> None:
        """Account takeover: provider email unverified -> refuse, no link."""
        victim = await make_user("victim@example.com")
        with pytest.raises(HTTPException) as exc:
            await self._callback(
                test_db,
                fake_client,
                {"id": "evil", "email": "victim@example.com", "email_verified": False},
            )
        assert exc.value.status_code == 400
        assert "link" in exc.value.detail
        links = (
            (
                await test_db.execute(
                    select(ExternalIdentity).where(
                        ExternalIdentity.user_id == victim.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert links == []

    @pytest.mark.asyncio
    async def test_missing_verified_flag_treated_as_unverified(
        self, test_db, fake_client
    ) -> None:
        with pytest.raises(HTTPException):
            await self._callback(
                test_db, fake_client, {"id": "x", "email": "new@example.com"}
            )
        assert (
            await test_db.execute(select(User).where(User.email == "new@example.com"))
        ).scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_unverified_local_account_never_links(
        self, test_db, fake_client, make_user
    ) -> None:
        """Pre-hijack: attacker pre-registered the email (never verified)."""
        squatter = await make_user("victim2@example.com", verified=False)
        with pytest.raises(HTTPException) as exc:
            await self._callback(
                test_db,
                fake_client,
                {"id": "g-v2", "email": "victim2@example.com", "email_verified": True},
            )
        assert exc.value.status_code == 409
        links = (
            (
                await test_db.execute(
                    select(ExternalIdentity).where(
                        ExternalIdentity.user_id == squatter.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert links == []

    @pytest.mark.asyncio
    async def test_both_verified_auto_links(
        self, test_db, fake_client, make_user
    ) -> None:
        user = await make_user("both@example.com")
        with patch.object(sso_mod, "record_audit") as audit:
            result = await self._callback(
                test_db,
                fake_client,
                {"id": "g-both", "email": "both@example.com", "email_verified": True},
            )
        assert isinstance(result, TokenPair)
        link = (
            await test_db.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
            )
        ).scalar_one()
        assert link.external_id == "g-both"
        assert audit.call_args.args[0] == "identity.sso.linked"

    @pytest.mark.asyncio
    async def test_inactive_user_by_email_rejected(
        self, test_db, fake_client, make_user
    ) -> None:
        await make_user("gone@example.com", active=False)
        with pytest.raises(HTTPException) as exc:
            await self._callback(
                test_db,
                fake_client,
                {"id": "g-gone", "email": "gone@example.com", "email_verified": True},
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_inactive_linked_user_rejected(
        self, test_db, fake_client, make_user
    ) -> None:
        user = await make_user("linked-gone@example.com")
        test_db.add(
            ExternalIdentity(
                user_id=user.id, provider=Provider.GOOGLE, external_id="g-lg"
            )
        )
        user.is_active = False
        await test_db.commit()
        with pytest.raises(HTTPException) as exc:
            await self._callback(
                test_db, fake_client, {"id": "g-lg", "email_verified": False}
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_github_without_verified_email_refused_cleanly(
        self, test_db, fake_client
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._callback(
                test_db,
                fake_client,
                {"id": "gh-1", "email": None, "email_verified": False},
            )
        assert exc.value.status_code == 400


class TestSecondFactor:
    async def _user_with_2fa(self, test_db, make_user, email):
        user = await make_user(email)
        init = await TwoFAService.init_2fa(
            user, test_db, current_password="TestPassword123!"
        )
        import time

        step = int(time.time()) // 30 - 1
        totp = pyotp.TOTP(init.secret)
        await TwoFAService.enable_2fa(
            user,
            totp.at(step * 30),
            test_db,
            current_password="TestPassword123!",
        )
        test_db.add(
            ExternalIdentity(
                user_id=user.id, provider=Provider.GOOGLE, external_id=f"g-{email}"
            )
        )
        await test_db.commit()
        return user, init

    @pytest.mark.asyncio
    async def test_sso_login_with_2fa_requires_second_factor(
        self, test_db, fake_client, make_user
    ) -> None:
        _user, init = await self._user_with_2fa(test_db, make_user, "mfa@example.com")
        fake_client({"id": "g-mfa@example.com", "email_verified": False})
        start = await _start()
        with patch.object(
            sso_mod.TokenService, "create_tokens", new=AsyncMock()
        ) as mint:
            result = await SSOService.handle_callback(
                "google", "c", start.state, test_db, binding=start.binding
            )
            assert isinstance(result, MFAChallengeOut)
            mint.assert_not_awaited()  # no tokens before the second factor

        with pytest.raises(HTTPException):
            await SSOService.complete_mfa(result.mfa_token, "000000", test_db)

        tokens = await SSOService.complete_mfa(
            result.mfa_token, pyotp.TOTP(init.secret).now(), test_db
        )
        assert isinstance(tokens, TokenPair)

        # The challenge is single-use.
        with pytest.raises(HTTPException):
            await SSOService.complete_mfa(
                result.mfa_token, init.backup_codes[0], test_db
            )

    @pytest.mark.asyncio
    async def test_mfa_challenge_rejects_deactivated_user(
        self, test_db, fake_client, make_user
    ) -> None:
        user, init = await self._user_with_2fa(test_db, make_user, "mfa2@example.com")
        fake_client({"id": "g-mfa2@example.com", "email_verified": False})
        start = await _start()
        result = await SSOService.handle_callback(
            "google", "c", start.state, test_db, binding=start.binding
        )
        user.is_active = False
        await test_db.commit()
        with pytest.raises(HTTPException) as exc:
            await SSOService.complete_mfa(
                result.mfa_token, pyotp.TOTP(init.secret).now(), test_db
            )
        assert exc.value.status_code == 403


class TestExplicitLink:
    @pytest.mark.asyncio
    async def test_link_requires_link_state_of_same_user(
        self, test_db, fake_client, make_user
    ) -> None:
        alice = await make_user("alice@example.com")
        bob = await make_user("bob@example.com")
        fake_client({"id": "g-link", "email": None, "email_verified": False})

        # A LOGIN state cannot be used to link.
        login_state = await _start()
        with pytest.raises(HTTPException):
            await SSOService.link_provider(
                alice,
                "google",
                "c",
                login_state.state,
                test_db,
                binding=login_state.binding,
                current_password="TestPassword123!",
            )

        # Bob's link state cannot link into Alice's account.
        bobs = await _start(purpose=PURPOSE_LINK, user_id=bob.id)
        with pytest.raises(HTTPException):
            await SSOService.link_provider(
                alice,
                "google",
                "c",
                bobs.state,
                test_db,
                binding=bobs.binding,
                current_password="TestPassword123!",
            )

        alices = await _start(purpose=PURPOSE_LINK, user_id=alice.id)
        with patch.object(sso_mod, "record_audit") as audit:
            await SSOService.link_provider(
                alice.id,
                "google",
                "c",
                alices.state,
                test_db,
                binding=alices.binding,
                current_password="TestPassword123!",
            )
        assert audit.call_args.args[0] == "identity.sso.linked"
        linked = await SSOService.get_user_linked_providers(alice.id, test_db)
        assert [i.external_id for i in linked] == ["g-link"]

    @pytest.mark.asyncio
    async def test_link_conflict_other_user(
        self, test_db, fake_client, make_user
    ) -> None:
        alice = await make_user("a2@example.com")
        bob = await make_user("b2@example.com")
        test_db.add(
            ExternalIdentity(
                user_id=bob.id, provider=Provider.GOOGLE, external_id="g-x"
            )
        )
        await test_db.commit()
        fake_client({"id": "g-x", "email_verified": False})
        st = await _start(purpose=PURPOSE_LINK, user_id=alice.id)
        with pytest.raises(HTTPException) as exc:
            await SSOService.link_provider(
                alice,
                "google",
                "c",
                st.state,
                test_db,
                binding=st.binding,
                current_password="TestPassword123!",
            )
        assert exc.value.status_code == 409


class TestUnlink:
    @pytest.mark.asyncio
    async def test_unlink_success_and_last_method_guard(
        self, test_db, make_user
    ) -> None:
        user = await make_user("unlink@example.com", password=None)
        test_db.add(
            ExternalIdentity(
                user_id=user.id, provider=Provider.GOOGLE, external_id="u1"
            )
        )
        await test_db.commit()
        with pytest.raises(HTTPException) as exc:
            await SSOService.unlink_provider(
                user,
                "google",
                test_db,
                token_claims={"auth_time": __import__("time").time()},
            )
        assert exc.value.status_code == 400

        from sandloom_identity.services.auth_service import AuthService

        user.hashed_password = AuthService.get_password_hash("TestPassword123!")
        await test_db.commit()
        with patch.object(sso_mod, "record_audit") as audit:
            await SSOService.unlink_provider(
                user.id, "google", test_db, current_password="TestPassword123!"
            )
        assert audit.call_args.args[0] == "identity.sso.unlinked"

    @pytest.mark.asyncio
    async def test_unlink_not_found(self, test_db, make_user) -> None:
        user = await make_user("unlink2@example.com")
        with pytest.raises(HTTPException) as exc:
            await SSOService.unlink_provider(
                user, "github", test_db, current_password="TestPassword123!"
            )
        assert exc.value.status_code == 404
