"""Seed tenancy state for billing tests through tenancy's PUBLIC HTTP API.

Billing must not import tenancy's code — not even in tests (``sandloom check``
rule ``test-imports``). Organizations and memberships are created the way a
client would: ``POST /api/v1/orgs``, an invitation accepted by the invitee,
role changes / removals / ownership transfers through tenancy's routes. The
harness ``client`` shares the test session, so the rows land in ``test_db``
(commit it when a route under test runs its own transactions).
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient

from sandloom.sdk.testing import as_user, email_of

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

_ORGS = "/api/v1/orgs"


def _ok(resp: Any, *expected: int) -> Any:
    assert resp.status_code in expected, (
        f"{resp.request.url}: {resp.status_code} {resp.text}"
    )
    return resp


async def create_org(
    client: AsyncClient,
    owner_headers: dict[str, str],
    *,
    name: str = "Org",
    slug: str | None = None,
) -> uuid.UUID:
    """A new organization owned by the bearer of ``owner_headers``."""
    resp = await client.post(
        _ORGS,
        json={"name": name, "slug": slug or f"org-{uuid.uuid4().hex[:8]}"},
        headers=owner_headers,
    )
    return uuid.UUID(_ok(resp, 201).json()["id"])


async def add_member(
    client: AsyncClient,
    org_id: uuid.UUID,
    owner_headers: dict[str, str],
    user_id: uuid.UUID,
    role: str = ROLE_MEMBER,
) -> None:
    """``user_id`` joins ``org_id`` with ``role``: invited by the owner, the
    invitation accepted with a verified e-mail claim."""
    invite_role = ROLE_ADMIN if role == ROLE_ADMIN else ROLE_MEMBER
    resp = await client.post(
        f"{_ORGS}/{org_id}/invitations",
        json={"email": email_of(user_id), "role": invite_role},
        headers=owner_headers,
    )
    token = _ok(resp, 201).json()["token"]
    _, invitee = as_user(user_id)
    resp = await client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=invitee
    )
    _ok(resp, 200)


async def change_role(
    client: AsyncClient,
    org_id: uuid.UUID,
    actor_headers: dict[str, str],
    user_id: uuid.UUID,
    role: str,
) -> None:
    resp = await client.patch(
        f"{_ORGS}/{org_id}/members/{user_id}",
        json={"role": role},
        headers=actor_headers,
    )
    _ok(resp, 200)


async def remove_member(
    client: AsyncClient,
    org_id: uuid.UUID,
    actor_headers: dict[str, str],
    user_id: uuid.UUID,
) -> None:
    resp = await client.delete(
        f"{_ORGS}/{org_id}/members/{user_id}", headers=actor_headers
    )
    _ok(resp, 204)


async def transfer_ownership(
    client: AsyncClient,
    org_id: uuid.UUID,
    owner_headers: dict[str, str],
    new_owner_id: uuid.UUID,
) -> None:
    resp = await client.post(
        f"{_ORGS}/{org_id}/transfer-ownership/{new_owner_id}", headers=owner_headers
    )
    _ok(resp, 200)
