"""End-to-end: minted access tokens carry authorization claims.

Proves the claims-contributor chain (identity contributor -> core.collect_claims
-> token_service mint) populates roles/permissions in the JWT, so the shared
claim atoms have data to read with zero per-request I/O.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from sandloom.sdk.security import decode_token
from sandloom_identity.models.user import User

pytestmark = pytest.mark.asyncio


async def test_login_token_carries_role_claims(
    async_client: AsyncClient, admin_user: User
) -> None:
    resp = await async_client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )
    assert resp.status_code == 200, resp.text
    payload = decode_token(resp.json()["access_token"])
    assert payload is not None
    # Contributor ran: both claim keys are present...
    assert "roles" in payload
    assert "permissions" in payload
    # ...and the user's assigned role is embedded.
    assert "admin" in payload["roles"]


async def test_login_token_without_roles_has_empty_claims(
    async_client: AsyncClient, test_user: User
) -> None:
    resp = await async_client.post(
        "/auth/login",
        json={"email": "testuser@example.com", "password": "TestPassword123!"},
    )
    assert resp.status_code == 200, resp.text
    payload = decode_token(resp.json()["access_token"])
    assert payload is not None
    # A user with no roles still gets the keys (fail-closed empty), proving the
    # contributor ran rather than the claims being merely absent.
    assert payload["roles"] == []
    assert payload["permissions"] == []
