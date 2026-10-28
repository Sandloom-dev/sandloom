"""``GET /auth/me``: one revocation lookup per request, same answers.

The framework's authenticator checks the token against its subject's state
record (ADR-0009: one live ``GET``, memoised per request), so the endpoint
pays ONE cache round trip; a revoked token is a ``401``.
"""

from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from sandloom.sdk.security import revoke_user_tokens
from sandloom.sdk.testing import as_user, record_revocation_checks
from sandloom_identity.models.user import User

pytestmark = pytest.mark.asyncio


async def test_me_checks_revocation_once(
    async_client: AsyncClient, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_revocation_checks(monkeypatch)
    _, headers = as_user(test_user.id, tenant=False)

    resp = await async_client.get("/auth/me", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(test_user.id)
    assert body["email"] == test_user.email
    assert calls == [str(test_user.id)]


async def test_me_rejects_revoked_token_as_invalid(
    async_client: AsyncClient, test_user: User
) -> None:
    _, headers = as_user(test_user.id, tenant=False)
    await revoke_user_tokens(test_user.id, valid_after=int(time.time()) + 5)

    resp = await async_client.get("/auth/me", headers=headers)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token has been revoked"
    assert resp.headers["www-authenticate"] == 'Bearer error="invalid_token"'
