"""HTTP-level SSO hardening: flow binding, 2FA step, authenticated linking."""

import time
from typing import Any
from unittest.mock import patch

import httpx
import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from sandloom_identity.models.external_identity import ExternalIdentity, Provider
from sandloom_identity.services import sso_service as sso_mod
from sandloom_identity.services.twofa_service import TwoFAService


class _Client:
    supports_pkce = True
    uses_nonce = False

    def __init__(self, info):
        self.info = info

    def get_auth_url(
        self, state, redirect_uri=None, *, code_challenge=None, nonce=None
    ):
        return f"https://idp.example/auth?state={state}"

    async def exchange_code_for_user_info(self, code, **kw):
        return dict(self.info)


@pytest.fixture
def idp():
    holder = {}

    def install(info):
        holder["c"] = _Client(info)

    with patch.object(
        sso_mod.OAuthClientFactory,
        "create_client",
        side_effect=lambda provider, redirect_uri=None: holder.get("c"),
    ):
        yield install


def _fresh_client(app: Any) -> AsyncClient:
    """A different browser: no cookies from the flow initiator."""
    return AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_login_csrf_victim_browser_cannot_complete_attacker_flow(
    async_client: AsyncClient, idp, sandloom_app: Any
) -> None:
    idp({"id": "g-att", "email": "att@example.com", "email_verified": True})
    start = await async_client.get("/auth/sso/google/redirect")
    state = start.json()["state"]

    # Victim's browser follows the attacker's callback link: no binding.
    async with _fresh_client(sandloom_app) as victim:
        resp = await victim.get(f"/auth/sso/google/callback?code=c&state={state}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_binding_via_header_for_api_clients(
    async_client: AsyncClient, idp, sandloom_app: Any
) -> None:
    idp({"id": "g-hdr", "email": "hdr@example.com", "email_verified": True})
    start = (await async_client.get("/auth/sso/google/redirect")).json()
    async with _fresh_client(sandloom_app) as spa:
        resp = await spa.get(
            f"/auth/sso/google/callback?code=c&state={start['state']}",
            headers={"X-SSO-Binding": start["binding"]},
        )
    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_sso_login_with_2fa_goes_through_mfa_endpoint(
    async_client: AsyncClient, test_db, make_user, idp
) -> None:
    user = await make_user("sso2fa@example.com")
    init = await TwoFAService.init_2fa(
        user, test_db, current_password="TestPassword123!"
    )
    step = int(time.time()) // 30 - 1
    await TwoFAService.enable_2fa(
        user,
        pyotp.TOTP(init.secret).at(step * 30),
        test_db,
        current_password="TestPassword123!",
    )
    test_db.add(
        ExternalIdentity(user_id=user.id, provider=Provider.GOOGLE, external_id="g-2fa")
    )
    await test_db.commit()

    idp({"id": "g-2fa", "email_verified": False})
    start = await async_client.get("/auth/sso/google/redirect")
    cb = await async_client.get(
        f"/auth/sso/google/callback?code=c&state={start.json()['state']}"
    )
    assert cb.status_code == 200
    body = cb.json()
    assert body["mfa_required"] is True
    assert "access_token" not in body

    bad = await async_client.post(
        "/auth/sso/mfa", json={"mfa_token": body["mfa_token"], "otp": "000000"}
    )
    assert bad.status_code == 401
    ok = await async_client.post(
        "/auth/sso/mfa",
        json={"mfa_token": body["mfa_token"], "otp": pyotp.TOTP(init.secret).now()},
    )
    assert ok.status_code == 200, ok.text
    assert "access_token" in ok.json()


@pytest.mark.asyncio
async def test_link_flow_requires_authenticated_link_state(
    async_client: AsyncClient, authenticated_headers, test_db, test_user, idp
) -> None:
    idp({"id": "g-link-api", "email": None, "email_verified": False})

    # A login state (e.g. obtained by an attacker) cannot be used to link.
    login_start = await async_client.get("/auth/sso/google/redirect")
    resp = await async_client.post(
        "/auth/sso/google/link",
        json={
            "code": "c",
            "state": login_start.json()["state"],
            "current_password": "TestPassword123!",
        },
        headers=authenticated_headers,
    )
    assert resp.status_code == 400

    unauth = await async_client.get("/auth/sso/google/link/redirect")
    assert unauth.status_code == 401

    link_start = await async_client.get(
        "/auth/sso/google/link/redirect", headers=authenticated_headers
    )
    assert link_start.status_code == 200
    resp = await async_client.post(
        "/auth/sso/google/link",
        json={
            "code": "c",
            "state": link_start.json()["state"],
            "current_password": "TestPassword123!",
        },
        headers=authenticated_headers,
    )
    assert resp.status_code == 204, resp.text
    linked = (
        await test_db.execute(
            select(ExternalIdentity).where(ExternalIdentity.user_id == test_user.id)
        )
    ).scalar_one()
    assert linked.external_id == "g-link-api"


async def _enable_2fa(user, test_db):
    init = await TwoFAService.init_2fa(
        user, test_db, current_password="TestPassword123!"
    )
    step = int(time.time()) // 30 - 1
    await TwoFAService.enable_2fa(
        user,
        pyotp.TOTP(init.secret).at(step * 30),
        test_db,
        current_password="TestPassword123!",
    )
    await test_db.commit()
    return init.secret


async def _links_for(test_db, user_id):
    test_db.expire_all()
    return (
        (
            await test_db.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_auto_link_is_deferred_until_mfa_succeeds(
    async_client: AsyncClient, test_db, make_user, idp
) -> None:
    """An email auto-link to a 2FA account must not commit before the 2FA step."""
    user = await make_user("autolink2fa@example.com")
    secret = await _enable_2fa(user, test_db)

    idp({"id": "g-al", "email": "autolink2fa@example.com", "email_verified": True})
    start = await async_client.get("/auth/sso/google/redirect")
    cb = await async_client.get(
        f"/auth/sso/google/callback?code=c&state={start.json()['state']}"
    )
    assert cb.status_code == 200, cb.text
    body = cb.json()
    assert body["mfa_required"] is True
    # The challenge response committed the request UoW: still no link.
    assert list(await _links_for(test_db, user.id)) == []

    bad = await async_client.post(
        "/auth/sso/mfa", json={"mfa_token": body["mfa_token"], "otp": "000000"}
    )
    assert bad.status_code == 401
    assert list(await _links_for(test_db, user.id)) == []

    ok = await async_client.post(
        "/auth/sso/mfa",
        json={"mfa_token": body["mfa_token"], "otp": pyotp.TOTP(secret).now()},
    )
    assert ok.status_code == 200, ok.text
    links = await _links_for(test_db, user.id)
    assert [link.external_id for link in links] == ["g-al"]


@pytest.mark.asyncio
async def test_mismatched_binding_does_not_burn_the_state(
    async_client: AsyncClient, idp, sandloom_app: Any
) -> None:
    """A wrong binding is rejected WITHOUT consuming the legitimate flow's state."""
    idp({"id": "g-burn", "email": "burn@example.com", "email_verified": True})
    start = (await async_client.get("/auth/sso/google/redirect")).json()
    async with _fresh_client(sandloom_app) as attacker:
        resp = await attacker.get(
            f"/auth/sso/google/callback?code=c&state={start['state']}",
            headers={"X-SSO-Binding": "not-the-binding"},
        )
    assert resp.status_code == 400
    # The initiator can still finish its own flow...
    ok = await async_client.get(
        f"/auth/sso/google/callback?code=c&state={start['state']}"
    )
    assert ok.status_code == 200, ok.text
    assert "access_token" in ok.json()
    # ...exactly once.
    again = await async_client.get(
        f"/auth/sso/google/callback?code=c&state={start['state']}"
    )
    assert again.status_code == 400
