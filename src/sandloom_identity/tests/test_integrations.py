"""Tests for the identity integrations (OAuth clients, notifier email sender)."""

import base64
import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from sandloom_identity.integrations import (
    EmailSender,
    GitHubOAuthClient,
    GoogleOAuthClient,
    LinkedInOAuthClient,
    MicrosoftOAuthClient,
    NotifierEmailSender,
    OAuthClient,
    OAuthClientFactory,
    TelegramOAuthClient,
    email_sender,
    make_pkce_pair,
    oauth_factory,
)
from sandloom_identity.models.external_identity import Provider


def _resp(data: Any) -> Mock:
    return Mock(raise_for_status=Mock(), json=Mock(return_value=data))


def _jwt(claims: dict[str, Any]) -> str:
    def b64(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


class TestAbstractClasses:
    def test_email_sender_abstract_methods(self) -> None:
        assert "send_verification_email" in EmailSender.__abstractmethods__
        assert "send_password_reset_email" in EmailSender.__abstractmethods__

    def test_oauth_client_abstract_methods(self) -> None:
        assert "get_auth_url" in OAuthClient.__abstractmethods__
        assert "exchange_code_for_user_info" in OAuthClient.__abstractmethods__


class TestNotifierEmailSender:
    """Mail goes through the framework notifier; links never hit stdout/logs."""

    @pytest.mark.asyncio
    async def test_verification_email_sent_via_notifier(self, capsys) -> None:
        fake = Mock(send=AsyncMock())
        with patch("sandloom_identity.integrations.notifier", return_value=fake):
            await NotifierEmailSender().send_verification_email(
                to_email="a@example.com",
                full_name="Ann",
                verification_url="https://app/verify?token=SECRET123",
            )
        notification = fake.send.await_args.args[0]
        assert notification.channel == "email"
        assert notification.recipient == "a@example.com"
        assert "SECRET123" in notification.body
        # The secret link travels ONLY in the body (never metadata / stdout).
        assert "SECRET123" not in json.dumps(dict(notification.data))
        assert "SECRET123" not in notification.subject
        assert "SECRET123" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_password_reset_email_sent_via_notifier(self) -> None:
        fake = Mock(send=AsyncMock())
        with patch("sandloom_identity.integrations.notifier", return_value=fake):
            await NotifierEmailSender().send_password_reset_email(
                to_email="a@example.com",
                full_name=None,
                reset_url="https://app/reset?token=RST",
            )
        notification = fake.send.await_args.args[0]
        assert "RST" in notification.body
        assert notification.data == {"kind": "password_reset"}

    @pytest.mark.asyncio
    async def test_a_driver_can_render_each_kind_from_its_body_fields(self) -> None:
        fake = Mock(send=AsyncMock())
        sender = NotifierEmailSender()
        with patch("sandloom_identity.integrations.notifier", return_value=fake):
            await sender.send_verification_email("a@example.com", "Ann", "https://v")
            await sender.send_password_reset_email("a@example.com", None, "https://r")
            await sender.send_registration_attempt_email(
                "a@example.com", "Ann", "https://s", "https://r"
            )
            await sender.send_security_notice("a@example.com", "New device")
        sent = [call.args[0] for call in fake.send.await_args_list]
        assert [(n.data["kind"], dict(n.body_fields)) for n in sent] == [
            (
                "email_verification",
                {"full_name": "Ann", "verification_url": "https://v"},
            ),
            ("password_reset", {"reset_url": "https://r"}),
            (
                "registration_attempt",
                {
                    "full_name": "Ann",
                    "sign_in_url": "https://s",
                    "reset_url": "https://r",
                },
            ),
            ("security_notice", {"message": "New device"}),
        ]
        # Sensitive like the body: never in the metadata a driver may record.
        assert all(set(n.data) == {"kind"} for n in sent)

    @pytest.mark.asyncio
    async def test_delivery_failure_is_swallowed_without_leaking_link(
        self, caplog
    ) -> None:
        fake = Mock(send=AsyncMock(side_effect=RuntimeError("smtp down LINK")))
        with patch("sandloom_identity.integrations.notifier", return_value=fake):
            await NotifierEmailSender().send_password_reset_email(
                to_email="a@example.com",
                full_name=None,
                reset_url="https://app/reset?token=TOPSECRET",
            )
        assert "TOPSECRET" not in caplog.text

    @pytest.mark.asyncio
    async def test_log_driver_does_not_log_body(self, caplog) -> None:
        """The default ``log`` notifier driver must not log the body (link)."""
        import logging

        caplog.set_level(logging.DEBUG)
        await NotifierEmailSender().send_password_reset_email(
            to_email="a@example.com",
            full_name=None,
            reset_url="https://app/reset?token=NEVERLOGGED",
        )
        for record in caplog.records:
            assert "NEVERLOGGED" not in record.getMessage()
            assert "NEVERLOGGED" not in json.dumps(record.__dict__, default=str)

    def test_global_sender_is_notifier_backed(self) -> None:
        assert isinstance(email_sender, NotifierEmailSender)


class TestPKCE:
    def test_pair_is_s256(self) -> None:
        verifier, challenge = make_pkce_pair()
        assert 43 <= len(verifier) <= 128
        digest = hashlib.sha256(verifier.encode()).digest()
        assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class TestGoogleOAuthClient:
    @pytest.fixture
    def google_client(self) -> GoogleOAuthClient:
        return GoogleOAuthClient(
            client_id="google_client_id",
            client_secret="google_client_secret",
            redirect_uri="https://app.example.com/auth/google/callback",
        )

    def test_get_auth_url_default_redirect(self, google_client) -> None:
        url = google_client.get_auth_url(state="test_state", code_challenge="CH")
        q = parse_qs(urlparse(url).query)
        assert urlparse(url).netloc == "accounts.google.com"
        assert q["redirect_uri"] == ["https://app.example.com/auth/google/callback"]
        assert q["scope"] == ["openid email profile"]
        assert q["state"] == ["test_state"]
        assert q["code_challenge"] == ["CH"]
        assert q["code_challenge_method"] == ["S256"]

    def test_get_auth_url_custom_redirect(self, google_client) -> None:
        url = google_client.get_auth_url(
            state="s", redirect_uri="https://custom.example.com/callback"
        )
        q = parse_qs(urlparse(url).query)
        assert q["redirect_uri"] == ["https://custom.example.com/callback"]
        assert "code_challenge" not in q

    @pytest.mark.asyncio
    async def test_exchange_uses_given_redirect_and_verifier(
        self, google_client
    ) -> None:
        with patch("httpx.AsyncClient") as mock_client:
            inst = mock_client.return_value.__aenter__.return_value
            inst.post.return_value = _resp({"access_token": "at"})
            inst.get.return_value = _resp(
                {
                    "id": "g1",
                    "email": "u@gmail.com",
                    "verified_email": True,
                    "name": "U",
                }
            )
            result = await google_client.exchange_code_for_user_info(
                "code",
                redirect_uri="https://custom.example.com/callback",
                code_verifier="VER",
            )
        assert result == {
            "id": "g1",
            "email": "u@gmail.com",
            "email_verified": True,
            "name": "U",
        }
        sent = inst.post.call_args.kwargs["data"]
        assert sent["redirect_uri"] == "https://custom.example.com/callback"
        assert sent["code_verifier"] == "VER"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"verified_email": False}, False),
            ({}, False),
            ({"email_verified": True}, True),
            ({"email_verified": "true"}, True),
        ],
    )
    async def test_verified_flag(self, google_client, payload, expected) -> None:
        with patch("httpx.AsyncClient") as mock_client:
            inst = mock_client.return_value.__aenter__.return_value
            inst.post.return_value = _resp({"access_token": "at"})
            inst.get.return_value = _resp({"id": "g1", "email": "u@x.com", **payload})
            result = await google_client.exchange_code_for_user_info("code")
        assert result["email_verified"] is expected

    @pytest.mark.asyncio
    async def test_token_request_failure(self, google_client) -> None:
        with patch("httpx.AsyncClient") as mock_client:
            inst = mock_client.return_value.__aenter__.return_value
            bad = Mock()
            bad.raise_for_status.side_effect = httpx.HTTPStatusError(
                "fail", request=Mock(), response=Mock()
            )
            inst.post.return_value = bad
            with pytest.raises(httpx.HTTPStatusError):
                await google_client.exchange_code_for_user_info("x")


class TestGitHubOAuthClient:
    @pytest.fixture
    def github_client(self) -> GitHubOAuthClient:
        return GitHubOAuthClient(
            client_id="github_client_id",
            client_secret="github_client_secret",
            redirect_uri="https://app.example.com/auth/github/callback",
        )

    def test_get_auth_url(self, github_client) -> None:
        url = github_client.get_auth_url(state="github_state", code_challenge="C")
        q = parse_qs(urlparse(url).query)
        assert q["scope"] == ["user:email"]
        assert q["code_challenge_method"] == ["S256"]

    async def _exchange(self, client, user_info, emails) -> dict[str, Any]:
        with patch("httpx.AsyncClient") as mock_client:
            inst = mock_client.return_value.__aenter__.return_value
            inst.post.return_value = _resp({"access_token": "t"})
            inst.get.side_effect = [_resp(user_info), _resp(emails)]
            return await client.exchange_code_for_user_info("c")

    @pytest.mark.asyncio
    async def test_uses_verified_primary_email(self, github_client) -> None:
        result = await self._exchange(
            github_client,
            {"id": 123, "name": "GH"},
            [
                {"email": "sec@example.com", "primary": False, "verified": True},
                {"email": "pri@example.com", "primary": True, "verified": True},
            ],
        )
        assert result == {
            "id": "123",
            "email": "pri@example.com",
            "email_verified": True,
            "name": "GH",
        }

    @pytest.mark.asyncio
    async def test_unverified_primary_is_not_used(self, github_client) -> None:
        result = await self._exchange(
            github_client,
            {"id": 123, "name": "GH", "email": "public@example.com"},
            [{"email": "pri@example.com", "primary": True, "verified": False}],
        )
        assert result["email"] is None
        assert result["email_verified"] is False

    @pytest.mark.asyncio
    async def test_public_profile_email_never_trusted(self, github_client) -> None:
        result = await self._exchange(
            github_client,
            {"id": 123, "email": "fallback@example.com"},
            [{"email": "x@example.com", "primary": False, "verified": True}],
        )
        assert result["email"] is None


class TestMicrosoftOAuthClient:
    @pytest.fixture
    def ms(self) -> MicrosoftOAuthClient:
        return MicrosoftOAuthClient(
            client_id="ms_client",
            client_secret="secret",
            redirect_uri="https://app.example.com/auth/microsoft/callback",
        )

    def test_get_auth_url_has_oidc_nonce_and_pkce(self, ms) -> None:
        url = ms.get_auth_url(state="s", code_challenge="C", nonce="N")
        q = parse_qs(urlparse(url).query)
        assert q["scope"] == ["openid email profile"]
        assert q["nonce"] == ["N"]
        assert q["code_challenge"] == ["C"]

    async def _exchange(self, ms, claims, nonce="N") -> dict[str, Any]:
        with patch("httpx.AsyncClient") as mock_client:
            inst = mock_client.return_value.__aenter__.return_value
            inst.post.return_value = _resp(
                {"access_token": "t", "id_token": _jwt(claims)}
            )
            return await ms.exchange_code_for_user_info(
                "c", code_verifier="V", nonce=nonce
            )

    @pytest.mark.asyncio
    async def test_identity_is_tid_oid_and_mail_not_trusted(self, ms) -> None:
        result = await self._exchange(
            ms,
            {
                "aud": "ms_client",
                "nonce": "N",
                "tid": "T1",
                "oid": "O1",
                "email": "victim@company.com",
                "name": "Eve",
            },
        )
        assert result["id"] == "T1:O1"
        # No xms_edov => attacker-controllable mail is NOT an identity claim.
        assert result["email"] is None
        assert result["email_verified"] is False

    @pytest.mark.asyncio
    async def test_xms_edov_makes_email_verified(self, ms) -> None:
        result = await self._exchange(
            ms,
            {
                "aud": "ms_client",
                "nonce": "N",
                "tid": "T1",
                "oid": "O1",
                "email": "u@company.com",
                "xms_edov": True,
            },
        )
        assert result["email"] == "u@company.com"
        assert result["email_verified"] is True

    @pytest.mark.asyncio
    async def test_nonce_mismatch_rejected(self, ms) -> None:
        with pytest.raises(ValueError, match="nonce"):
            await self._exchange(
                ms, {"aud": "ms_client", "nonce": "OTHER", "tid": "T", "oid": "O"}
            )

    @pytest.mark.asyncio
    async def test_audience_mismatch_rejected(self, ms) -> None:
        with pytest.raises(ValueError, match="audience"):
            await self._exchange(
                ms, {"aud": "someone_else", "nonce": "N", "tid": "T", "oid": "O"}
            )


class TestLinkedInOAuthClient:
    @pytest.fixture
    def li(self) -> LinkedInOAuthClient:
        return LinkedInOAuthClient("li_id", "li_secret", "https://app/cb")

    def test_get_auth_url(self, li) -> None:
        q = parse_qs(urlparse(li.get_auth_url(state="s")).query)
        assert q["scope"] == ["openid profile email"]

    @pytest.mark.asyncio
    async def test_exchange_oidc_userinfo(self, li) -> None:
        with patch("httpx.AsyncClient") as mock_client:
            inst = mock_client.return_value.__aenter__.return_value
            inst.post.return_value = _resp({"access_token": "t"})
            inst.get.return_value = _resp(
                {
                    "sub": "li1",
                    "email": "j@x.com",
                    "email_verified": True,
                    "given_name": "J",
                    "family_name": "D",
                }
            )
            result = await li.exchange_code_for_user_info("c")
        assert result == {
            "id": "li1",
            "email": "j@x.com",
            "email_verified": True,
            "name": "J D",
        }


class TestTelegramOAuthClient:
    def test_get_auth_url(self) -> None:
        tg = TelegramOAuthClient("123456789:ABC", "https://app/cb")
        q = parse_qs(urlparse(tg.get_auth_url(state="s")).query)
        assert q["bot_id"] == ["123456789"]

    @pytest.mark.asyncio
    async def test_exchange_not_implemented(self) -> None:
        tg = TelegramOAuthClient("1:A", "https://app/cb")
        with pytest.raises(NotImplementedError):
            await tg.exchange_code_for_user_info("c")


class TestOAuthClientFactory:
    def test_create_client_missing_credentials_returns_none(self) -> None:
        assert OAuthClientFactory.create_client(Provider.MICROSOFT) is None

    def test_oauth_factory_instance(self) -> None:
        assert isinstance(oauth_factory, OAuthClientFactory)
