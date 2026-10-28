"""Emergency user block on the token ISSUERS (real Postgres + Redis).

``block_user`` (sandloom.core.di) must stop password login, SSO / 2FA completion
and the refresh-token exchange — ``401 "Account blocked"``, no token minted,
no refresh rotation side effect — with a live check; a cache outage fails
closed per ``AUTH_REVOCATION_FAIL_CLOSED``. The block also ends the user's
sessions: access tokens are revoked on commit and identity's consumer of
``core.auth.user_blocked.v1`` revokes every refresh token.
"""

from __future__ import annotations

import time
from typing import Any

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.infra import settings
from sandloom.sdk.security import block_user, unblock_user
from sandloom.sdk.testing import InMemoryCache, run_on_commit, use_security_cache
from sandloom_identity.consumers import revoke_refresh_tokens_on_block
from sandloom_identity.models.external_identity import ExternalIdentity, Provider
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.user import User
from sandloom_identity.services.twofa_service import TwoFAService

from .test_sso_hardening import idp  # noqa: F401 - fixture reuse

PASSWORD = "TestPassword123!"


async def _login(client: AsyncClient, user: User) -> Any:
    return await client.post(
        "/auth/login", json={"email": user.email, "password": PASSWORD}
    )


async def _refresh_rows(db: AsyncSession, user: User) -> list[tuple[Any, bool]]:
    rows = (
        await db.execute(
            select(RefreshToken.id, RefreshToken.is_revoked)
            .where(RefreshToken.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return sorted((r.id, r.is_revoked) for r in rows)


async def _commit(*sessions: Any) -> None:
    """Treat the test's UoW(s) as committed (``unblock_user`` lifts the mark
    on commit): the in-test requests may leave either session bound."""
    for session in sessions:
        await run_on_commit(session)


def _assert_blocked(resp: Any) -> None:
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Account blocked"
    assert "access_token" not in resp.json()


@pytest.mark.asyncio
async def test_blocked_user_cannot_login_or_refresh(
    async_client: AsyncClient, test_user: User, test_db: AsyncSession, _ambient_uow: Any
) -> None:
    first = await _login(async_client, test_user)
    assert first.status_code == 200
    refresh = first.json()["refresh_token"]
    access_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    before = await _refresh_rows(test_db, test_user)

    await block_user(test_user.id, reason="credential leak")

    _assert_blocked(await _login(async_client, test_user))
    _assert_blocked(
        await async_client.post("/auth/refresh", json={"refresh_token": refresh})
    )
    # No rotation side effect: the presented token is still live, nothing new.
    assert await _refresh_rows(test_db, test_user) == before
    # The access token obtained before the block is refused too.
    resp = await async_client.get("/auth/me", headers=access_headers)
    assert resp.status_code == 401

    # Unblocked (and the session-ending consumer NOT run): the old refresh
    # token works again — the block itself had no side effect on it.
    await unblock_user(test_user.id, reason="false alarm")
    await _commit(test_db, _ambient_uow)  # the mark is lifted after commit
    ok = await async_client.post("/auth/refresh", json={"refresh_token": refresh})
    assert ok.status_code == 200, ok.text
    assert (await _login(async_client, test_user)).status_code == 200


@pytest.mark.asyncio
async def test_block_ends_sessions_on_commit(
    async_client: AsyncClient, test_user: User, test_db: AsyncSession, _ambient_uow: Any
) -> None:
    first = await _login(async_client, test_user)
    refresh = first.json()["refresh_token"]
    old_access = {"Authorization": f"Bearer {first.json()['access_token']}"}
    time.sleep(1.05)  # tokens minted in the revocation's own second stay valid

    await block_user(test_user.id, reason="compromised", actor_id=None)
    # The audit and the event went into the (recording) UoW.
    assert "auth.user.blocked" in _ambient_uow.audit_actions()
    [(_channel, payload, _v)] = [
        e for e in _ambient_uow.events if e[0] == "core.auth.user_blocked.v1"
    ]
    assert payload["user_id"] == str(test_user.id)
    # Commit: access tokens revoked; identity's consumer revokes refresh tokens.
    await run_on_commit(_ambient_uow)
    await revoke_refresh_tokens_on_block(test_db, {"payload": payload})
    await test_db.commit()

    await unblock_user(test_user.id, reason="rotated credentials")
    await _commit(test_db, _ambient_uow)  # the mark is lifted after commit
    # Lifting the block does not revive the old sessions.
    assert all(revoked for _id, revoked in await _refresh_rows(test_db, test_user))
    resp = await async_client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert resp.json()["detail"] != "Account blocked"
    assert (await async_client.get("/auth/me", headers=old_access)).status_code == 401
    # A fresh login works.
    fresh = await _login(async_client, test_user)
    assert fresh.status_code == 200
    headers = {"Authorization": f"Bearer {fresh.json()['access_token']}"}
    assert (await async_client.get("/auth/me", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_blocked_user_cannot_complete_sso_or_sso_2fa(
    async_client: AsyncClient,
    test_db: AsyncSession,
    make_user: Any,
    idp: Any,  # noqa: F811
    _ambient_uow: Any,
) -> None:
    # Plain SSO login.
    plain = await make_user("ssoblock@example.com")
    test_db.add(
        ExternalIdentity(user_id=plain.id, provider=Provider.GOOGLE, external_id="g-b1")
    )
    await test_db.commit()
    await block_user(plain.id, reason="leak")
    idp({"id": "g-b1", "email_verified": False})
    start = await async_client.get("/auth/sso/google/redirect")
    cb = await async_client.get(
        f"/auth/sso/google/callback?code=c&state={start.json()['state']}"
    )
    _assert_blocked(cb)

    # SSO + second factor: the challenge is issued, its completion refused.
    user = await make_user("sso2fablock@example.com")
    init = await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
    step = int(time.time()) // 30 - 1
    await TwoFAService.enable_2fa(
        user, pyotp.TOTP(init.secret).at(step * 30), test_db, current_password=PASSWORD
    )
    test_db.add(
        ExternalIdentity(user_id=user.id, provider=Provider.GOOGLE, external_id="g-b2")
    )
    await test_db.commit()
    idp({"id": "g-b2", "email_verified": False})
    start = await async_client.get("/auth/sso/google/redirect")
    cb = await async_client.get(
        f"/auth/sso/google/callback?code=c&state={start.json()['state']}"
    )
    body = cb.json()
    await block_user(user.id, reason="leak")
    resp = await async_client.post(
        "/auth/sso/mfa",
        json={"mfa_token": body["mfa_token"], "otp": pyotp.TOTP(init.secret).now()},
    )
    _assert_blocked(resp)
    # Unblocked, the SAME challenge still completes (it was not consumed).
    await unblock_user(user.id, reason="ok")
    await _commit(test_db, _ambient_uow)  # the mark is lifted after commit
    resp = await async_client.post(
        "/auth/sso/mfa",
        json={"mfa_token": body["mfa_token"], "otp": pyotp.TOTP(init.secret).now()},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_blocked_user_cannot_login_with_password_2fa(
    async_client: AsyncClient, test_db: AsyncSession, make_user: Any, _ambient_uow: Any
) -> None:
    user = await make_user("pw2fablock@example.com")
    init = await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
    step = int(time.time()) // 30 - 1
    await TwoFAService.enable_2fa(
        user, pyotp.TOTP(init.secret).at(step * 30), test_db, current_password=PASSWORD
    )
    await test_db.commit()
    await block_user(user.id, reason="leak")
    resp = await async_client.post(
        "/auth/login",
        json={
            "email": user.email,
            "password": PASSWORD,
            "otp": pyotp.TOTP(init.secret).now(),
        },
    )
    _assert_blocked(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_closed", [True, False])
async def test_issuer_block_check_on_cache_outage(
    test_user: User,
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    fail_closed: bool,
) -> None:
    """Redis down: issuance fails closed (503) or proceeds (fail-open)."""
    from fastapi import HTTPException

    from sandloom_identity.services.token_service import TokenService

    monkeypatch.setattr(settings, "auth_revocation_fail_closed", fail_closed)
    use_security_cache(monkeypatch, InMemoryCache(broken=True))
    if fail_closed:
        with pytest.raises(HTTPException) as exc:
            await TokenService.create_tokens(test_user, test_db)
        assert exc.value.status_code == 503
        assert await _refresh_rows(test_db, test_user) == []
    else:
        pair = await TokenService.create_tokens(test_user, test_db)
        assert pair.access_token
