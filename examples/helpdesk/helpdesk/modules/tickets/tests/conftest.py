"""Tickets-only fixtures: seeded organizations (the harness comes from the
framework's pytest plugin, ``sandloom.sdk.testing.plugin``: ``client`` / ``db``)."""

import uuid
from typing import Any

import pytest_asyncio
from httpx import AsyncClient

from sandloom.sdk.testing import (
    as_user,
    email_of,  # sandloom: advanced the as_user e-mail claim
)


async def _seed_org_member(
    client: AsyncClient, role: str
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    """Create an organization + a member of the given role.

    Goes through the tenancy module's PUBLIC HTTP API, like any client: an
    owner creates the org and invites the user, who accepts with a verified
    e-mail claim. The harness ``client`` shares the test session, so the rows
    land in ``db``. Neither the tickets module nor its tests import tenancy.
    """
    _, owner = as_user()
    resp = await client.post(
        "/api/v1/orgs",
        json={"name": "Acme Inc", "slug": f"acme-{uuid.uuid4().hex[:8]}"},
        headers=owner,
    )
    assert resp.status_code == 201, resp.text
    org_id = uuid.UUID(resp.json()["id"])

    user_id, headers = as_user()
    resp = await client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": email_of(user_id), "role": role},
        headers=owner,
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/v1/invitations/accept",
        json={"token": resp.json()["token"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return org_id, user_id, headers


@pytest_asyncio.fixture
async def member_org(
    client: AsyncClient,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    """An org where the caller is a plain member."""
    return await _seed_org_member(client, "member")


@pytest_asyncio.fixture
async def admin_org(client: AsyncClient) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    """An org where the caller is an admin (may change ticket status)."""
    return await _seed_org_member(client, "admin")
