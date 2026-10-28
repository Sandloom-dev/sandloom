"""Identity side of framework-wide token revocation, the ``email_verified``
claim, proxy-aware client IPs and the production 2FA-key requirement."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import AsyncClient

from sandloom.sdk.infra import settings
from sandloom.sdk.security import decode_token
from sandloom.sdk.testing import revocation_mark_key, set_environment
from sandloom_identity.models.user import User
from sandloom_identity.services.token_service import TokenService

pytestmark = pytest.mark.asyncio


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


async def test_access_token_carries_email_verified(
    async_client: AsyncClient, test_user: User
) -> None:
    token = await _login(async_client, "testuser@example.com", "TestPassword123!")
    claims = decode_token(token)
    assert claims is not None
    assert claims["email"] == "testuser@example.com"
    assert claims["email_verified"] is True


async def test_unverified_user_and_contributors_cannot_override_identity() -> None:
    user = Mock(spec=User)
    user.id = "00000000-0000-0000-0000-000000000001"
    user.email = "u@example.com"
    user.is_verified = False
    core = SimpleNamespace(
        collect_claims=AsyncMock(
            return_value={"sub": "attacker", "email_verified": True, "roles": ["r"]}
        )
    )
    db = AsyncMock()
    db.add = Mock()
    pair = await TokenService.create_tokens(user, db, core)  # type: ignore[arg-type]
    claims = decode_token(pair.access_token)
    assert claims is not None
    assert claims["sub"] == str(user.id)
    assert claims["email_verified"] is False
    assert claims["roles"] == ["r"]


async def test_revoke_sessions_uses_the_framework_mark(
    async_client: AsyncClient, test_user: User, test_redis: Any, _ambient_uow: Any
) -> None:
    from sandloom.sdk.testing import run_on_commit
    from sandloom_identity.sessions import revoke_sessions

    token = await _login(async_client, "testuser@example.com", "TestPassword123!")
    claims = decode_token(token)
    assert claims is not None
    auth = {"Authorization": f"Bearer {token}"}
    assert (await async_client.get("/auth/me", headers=auth)).status_code == 200

    await revoke_sessions(test_user.id, reason="test", publish=False)
    # Immediate (require_immediate): the mark is written BEFORE the commit —
    # a cache outage would have refused the change (503) instead.
    assert await test_redis.get(revocation_mark_key(test_user.id)) is not None
    await run_on_commit(_ambient_uow)
    mark = await test_redis.get(revocation_mark_key(test_user.id))
    assert mark is not None
    assert await test_redis.get(f"identity:tokens_valid_after:{test_user.id}") is None

    # Make the token strictly older than the mark (same-second tokens survive).
    await test_redis.set(revocation_mark_key(test_user.id), str(int(claims["iat"]) + 1))
    resp = await async_client.get("/auth/me", headers=auth)
    assert resp.status_code == 401


async def test_auth_audit_ip_is_proxy_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom_identity.api.auth import _ip

    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_ips", ["10.0.0.0/8"])
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.1.2.3"),
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert _ip(request) == "203.0.113.9"  # type: ignore[arg-type]
    assert _ip(SimpleNamespace(client=None, headers={})) is None  # type: ignore[arg-type]


async def test_twofa_key_is_mandatory_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom_identity import register
    from sandloom_identity.services import twofa_service

    monkeypatch.setattr(settings, "twofa_encryption_key", None)
    set_environment(monkeypatch, "dev")
    assert twofa_service.twofa_configuration_error() is None
    assert twofa_service._encrypt_secret("JBSWY3DPEHPK3PXP")  # dev fallback works

    set_environment(monkeypatch, "prod")
    error = twofa_service.twofa_configuration_error()
    assert error is not None and "TWOFA_ENCRYPTION_KEY" in error
    with pytest.raises(RuntimeError, match="TWOFA_ENCRYPTION_KEY"):
        twofa_service._encrypt_secret("JBSWY3DPEHPK3PXP")  # no JWT fallback

    recorder = Mock()
    register(recorder)
    calls = [
        c
        for c in recorder.add_startup_task.call_args_list
        if c.kwargs == {"critical": True}
    ]
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="TWOFA_ENCRYPTION_KEY"):
        await calls[0].args[0]()

    monkeypatch.setattr(settings, "twofa_encryption_key", "k" * 64)
    assert twofa_service.twofa_configuration_error() is None
