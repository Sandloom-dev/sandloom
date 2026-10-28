"""Operator account-safety commands: block, unblock, revoke sessions, search.

Real Postgres + Redis through the HTTP surface: platform-admin gate, class-A
re-check, fail-closed ``Idempotency-Key`` dedupe and the required audit row.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.user import User

from ..conftest import _login_headers

REASON = {"reason": "credential leak, ticket SEC-1"}


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _audit_actions(db: AsyncSession, user: User) -> list[str]:
    rows = await db.execute(
        text(
            "SELECT action FROM audit_log WHERE resource_id = :rid ORDER BY occurred_at"
        ),
        {"rid": str(user.id)},
    )
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_block_status_unblock_round_trip(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_headers: dict[str, str],
) -> None:
    base = f"/api/v1/users/{test_user.id}"
    status = await async_client.get(f"{base}/block", headers=admin_headers)
    assert status.status_code == 200, status.text
    assert status.json() == {"id": str(test_user.id), "blocked": False}

    blocked = await async_client.post(
        f"{base}/block", json=REASON, headers={**admin_headers, **_key()}
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["blocked"] is True
    status = await async_client.get(f"{base}/block", headers=admin_headers)
    assert status.json()["blocked"] is True
    login = await async_client.post(
        "/auth/login",
        json={"email": test_user.email, "password": "TestPassword123!"},
    )
    assert login.status_code == 401

    lifted = await async_client.post(
        f"{base}/unblock", json=REASON, headers={**admin_headers, **_key()}
    )
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["blocked"] is False
    assert await _audit_actions(test_db, test_user) == [
        "auth.user.blocked",
        "auth.user.unblocked",
    ]


@pytest.mark.asyncio
async def test_block_requires_idempotency_key_and_replays_once(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_headers: dict[str, str],
) -> None:
    url = f"/api/v1/users/{test_user.id}/block"
    missing = await async_client.post(url, json=REASON, headers=admin_headers)
    assert missing.status_code == 400

    headers = {**admin_headers, **_key()}
    first = await async_client.post(url, json=REASON, headers=headers)
    again = await async_client.post(url, json=REASON, headers=headers)
    assert first.status_code == again.status_code == 200
    assert again.headers.get("Idempotent-Replay") == "true"
    assert await _audit_actions(test_db, test_user) == ["auth.user.blocked"]

    other = await async_client.post(
        url, json={"reason": "a different reason"}, headers=headers
    )
    assert other.status_code == 422
    await async_client.post(
        f"/api/v1/users/{test_user.id}/unblock",
        json=REASON,
        headers={**admin_headers, **_key()},
    )


@pytest.mark.asyncio
async def test_operator_cannot_block_or_end_own_sessions(
    async_client: AsyncClient, admin_user: User, admin_headers: dict[str, str]
) -> None:
    for command in ("block", "revoke-sessions"):
        resp = await async_client.post(
            f"/api/v1/users/{admin_user.id}/{command}",
            json=REASON,
            headers={**admin_headers, **_key()},
        )
        assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_safety_commands_require_platform_admin(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    sandloom_app: Any,
) -> None:
    headers = await _login_headers(test_user, test_db, sandloom_app.state.core)
    for method, suffix in (
        ("GET", "block"),
        ("POST", "block"),
        ("POST", "unblock"),
        ("POST", "revoke-sessions"),
    ):
        resp = await async_client.request(
            method,
            f"/api/v1/users/{uuid4()}/{suffix}",
            json=REASON if method == "POST" else None,
            headers={**headers, **_key()},
        )
        assert resp.status_code == 403, (suffix, resp.text)


@pytest.mark.asyncio
async def test_unknown_user_is_404_and_reason_is_validated(
    async_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    url = f"/api/v1/users/{uuid4()}/block"
    assert (
        await async_client.post(url, json=REASON, headers={**admin_headers, **_key()})
    ).status_code == 404
    short = await async_client.post(
        url, json={"reason": "x"}, headers={**admin_headers, **_key()}
    )
    assert short.status_code == 422


@pytest.mark.asyncio
async def test_revoke_sessions_revokes_refresh_tokens_and_audits(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_headers: dict[str, str],
) -> None:
    login = await async_client.post(
        "/auth/login",
        json={"email": test_user.email, "password": "TestPassword123!"},
    )
    assert login.status_code == 200, login.text
    resp = await async_client.post(
        f"/api/v1/users/{test_user.id}/revoke-sessions",
        json=REASON,
        headers={**admin_headers, **_key()},
    )
    assert resp.status_code == 204, resp.text
    revoked = (
        await test_db.execute(
            select(RefreshToken.is_revoked)
            .where(RefreshToken.user_id == test_user.id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    assert all(revoked)
    assert await _audit_actions(test_db, test_user) == [
        "identity.user.sessions_revoked"
    ]
    refreshed = await async_client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert refreshed.status_code == 401


@pytest.mark.asyncio
async def test_user_search_is_a_literal_substring(
    async_client: AsyncClient,
    test_db: AsyncSession,
    test_user: User,
    admin_headers: dict[str, str],
) -> None:
    found = await async_client.get(
        "/api/v1/users", params={"q": "testuser@"}, headers=admin_headers
    )
    assert [u["id"] for u in found.json()] == [str(test_user.id)]
    wildcard = await async_client.get(
        "/api/v1/users", params={"q": "%"}, headers=admin_headers
    )
    assert wildcard.status_code == 200
    assert wildcard.json() == []
    inactive = await async_client.get(
        "/api/v1/users", params={"is_active": "false"}, headers=admin_headers
    )
    assert inactive.json() == []
