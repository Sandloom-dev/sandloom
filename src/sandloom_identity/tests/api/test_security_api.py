"""HTTP-level identity security behaviour."""

import time

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from sandloom.sdk.security import create_access_token
from sandloom_identity import lockout
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.role import Role
from sandloom_identity.models.user_role import UserRole

PW = "TestPassword123!"


@pytest.mark.asyncio
async def test_refresh_reuse_returns_401_and_revokes_family(
    async_client: AsyncClient, test_db, test_user
) -> None:
    login = await async_client.post(
        "/auth/login", json={"email": test_user.email, "password": PW}
    )
    first = login.json()["refresh_token"]
    rotated = await async_client.post("/auth/refresh", json={"refresh_token": first})
    assert rotated.status_code == 200
    # Within the rotation grace window a replay is a benign race (409) ...
    early = await async_client.post("/auth/refresh", json={"refresh_token": first})
    assert early.status_code == 409
    # ... beyond it, reuse revokes the whole family.
    from sandloom_identity.tests.services.test_refresh_rotation import age_rotation

    await age_rotation(test_db, first)

    replay = await async_client.post("/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Invalid or expired refresh token"

    test_db.expire_all()
    rows = (await test_db.execute(select(RefreshToken))).scalars().all()
    assert rows and all(r.is_revoked for r in rows)
    second = rotated.json()["refresh_token"]
    again = await async_client.post("/auth/refresh", json={"refresh_token": second})
    assert again.status_code == 401


@pytest.mark.asyncio
async def test_login_lockout_via_api(async_client: AsyncClient, test_user) -> None:
    for _ in range(lockout.FAIL_THRESHOLD):
        r = await async_client.post(
            "/auth/login", json={"email": test_user.email, "password": "wrong-pass"}
        )
        assert r.status_code == 401
    r = await async_client.post(
        "/auth/login", json={"email": test_user.email, "password": PW}
    )
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_login_no_longer_returns_dead_200_error(
    async_client: AsyncClient, test_user
) -> None:
    r = await async_client.post(
        "/auth/login", json={"email": test_user.email, "password": PW}
    )
    assert r.status_code == 200
    assert "access_token" in r.json()


@pytest.mark.asyncio
async def test_change_password_endpoint(
    async_client: AsyncClient, test_user, authenticated_headers
) -> None:
    bad = await async_client.post(
        "/auth/change-password",
        json={"current_password": "nope", "new_password": "BrandNewPass#1"},
        headers=authenticated_headers,
    )
    assert bad.status_code == 401
    ok = await async_client.post(
        "/auth/change-password",
        json={"current_password": PW, "new_password": "BrandNewPass#1"},
        headers=authenticated_headers,
    )
    assert ok.status_code == 200, ok.text
    assert "access_token" in ok.json()
    login = await async_client.post(
        "/auth/login",
        json={"email": test_user.email, "password": "BrandNewPass#1"},
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_role_revocation_is_immediate_on_identity_endpoints(
    async_client: AsyncClient, test_db, admin_user, admin_headers, test_user
) -> None:
    """A demoted admin's still-valid token is rejected by identity's guards."""
    # Mint the admin's token "in the past" so the revocation mark postdates it.
    old_token = create_access_token(
        data={
            "sub": str(admin_user.id),
            "token_type": "access",
            "roles": ["admin"],
            "permissions": [],
            "iat": int(time.time()) - 10,
        }
    )
    headers = {"Authorization": f"Bearer {old_token}"}
    assert (await async_client.get("/api/v1/roles", headers=headers)).status_code == 200

    admin_role = (
        await test_db.execute(select(Role).where(Role.code == "admin"))
    ).scalar_one()
    # Another admin revokes it.
    other = UserRole(user_id=test_user.id, role_id=admin_role.id)
    test_db.add(other)
    await test_db.commit()
    revoke = await async_client.delete(
        f"/api/v1/users/{admin_user.id}/roles/{admin_role.id}", headers=admin_headers
    )
    assert revoke.status_code == 204

    after = await async_client.get("/api/v1/roles", headers=headers)
    assert after.status_code == 401


@pytest.mark.asyncio
async def test_user_roles_endpoint_records_actor(
    async_client: AsyncClient, test_db, admin_user, admin_headers, test_user
) -> None:
    role = Role(name="Editor", code="editor", description="", is_system=False)
    test_db.add(role)
    await test_db.commit()
    user_id, role_id, admin_id = test_user.id, role.id, admin_user.id
    r = await async_client.post(
        f"/api/v1/users/{user_id}/roles/{role_id}", headers=admin_headers
    )
    assert r.status_code == 204
    test_db.expire_all()
    assigned_by = (
        await test_db.execute(
            select(UserRole.assigned_by).where(
                UserRole.user_id == user_id, UserRole.role_id == role_id
            )
        )
    ).scalar_one()
    assert assigned_by == admin_id
