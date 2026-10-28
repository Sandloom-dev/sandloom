"""Keyset pagination of the tenancy list endpoints (opaque cursors)."""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_tenancy.models import OrganizationMember


async def _ids(resp: Any) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()]


@pytest.mark.asyncio
async def test_members_keyset_pages_cover_everyone_once(
    async_client: AsyncClient,
    test_db: AsyncSession,
    user_a: tuple[uuid.UUID, dict[str, Any]],
    org_with_owner: dict[str, Any],
) -> None:
    _, headers = user_a
    org_id = uuid.UUID(org_with_owner["id"])
    test_db.add_all(
        OrganizationMember(organization_id=org_id, user_id=uuid.uuid4(), role="member")
        for _ in range(4)
    )
    await test_db.flush()
    url = f"/api/v1/orgs/{org_id}/members"

    everyone = await _ids(await async_client.get(url, headers=headers))
    assert len(everyone) == 5  # no limit: one default page (100)

    pages: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        resp = await async_client.get(url, headers=headers, params=params)
        page = await _ids(resp)
        assert len(page) <= 2
        pages.extend(page)
        cursor = resp.headers.get("X-Next-Cursor")
        if cursor is None:
            break
    assert pages == everyone
    over = await async_client.get(url, headers=headers, params={"limit": 501})
    assert over.status_code == 422


@pytest.mark.asyncio
async def test_my_orgs_keyset_pages(
    async_client: AsyncClient, user_a: tuple[uuid.UUID, dict[str, Any]]
) -> None:
    _, headers = user_a
    for i in range(3):
        resp = await async_client.post(
            "/api/v1/orgs",
            json={"name": f"Org {i}", "slug": f"pg-{uuid.uuid4().hex[:10]}"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
    everyone = await _ids(await async_client.get("/api/v1/orgs", headers=headers))
    assert len(everyone) == 3
    head = await async_client.get("/api/v1/orgs", headers=headers, params={"limit": 2})
    first = await _ids(head)
    rest = await _ids(
        await async_client.get(
            "/api/v1/orgs",
            headers=headers,
            params={"limit": 2, "cursor": head.headers["X-Next-Cursor"]},
        )
    )
    assert first + rest == everyone


@pytest.mark.asyncio
async def test_members_opaque_cursor_pages(
    async_client: AsyncClient,
    test_db: AsyncSession,
    user_a: tuple[uuid.UUID, dict[str, Any]],
    org_with_owner: dict[str, Any],
) -> None:
    """The ``X-Next-Cursor`` header chains pages; it is absent on the last one."""
    _, headers = user_a
    org_id = uuid.UUID(org_with_owner["id"])
    test_db.add_all(
        OrganizationMember(organization_id=org_id, user_id=uuid.uuid4(), role="member")
        for _ in range(4)
    )
    await test_db.flush()
    url = f"/api/v1/orgs/{org_id}/members"
    everyone = await _ids(await async_client.get(url, headers=headers))

    pages: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        resp = await async_client.get(url, headers=headers, params=params)
        pages.extend(await _ids(resp))
        cursor = resp.headers.get("X-Next-Cursor")
        if cursor is None:
            break
    assert pages == everyone

    first = await async_client.get(url, headers=headers, params={"limit": 2})
    good = first.headers["X-Next-Cursor"]
    for bad in (good[:-2] + ("AA" if not good.endswith("AA") else "BB"), "x.y", "x"):
        resp = await async_client.get(
            url, headers=headers, params={"limit": 2, "cursor": bad}
        )
        assert resp.status_code == 400, (bad, resp.text)


@pytest.mark.asyncio
async def test_cursor_is_bound_to_its_organization_and_user(
    async_client: AsyncClient,
    user_a: tuple[uuid.UUID, dict[str, Any]],
    user_b: tuple[uuid.UUID, dict[str, Any]],
) -> None:
    """A cursor minted for one organization (or one user's org list) is
    refused for another (400): it cannot be replayed across filters."""
    _, headers = user_a
    _, headers_b = user_b
    orgs = []
    for i in range(3):
        resp = await async_client.post(
            "/api/v1/orgs",
            json={"name": f"Cur {i}", "slug": f"cur-{uuid.uuid4().hex[:10]}"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        orgs.append(resp.json()["id"])
    mine = await async_client.get("/api/v1/orgs", headers=headers, params={"limit": 1})
    cursor = mine.headers["X-Next-Cursor"]
    # user B replays user A's org-list cursor
    replay = await async_client.get(
        "/api/v1/orgs", headers=headers_b, params={"limit": 1, "cursor": cursor}
    )
    assert replay.status_code == 400
    # a member-list cursor of org 0 against org 1 (both A's)
    resp = await async_client.get(
        f"/api/v1/orgs/{orgs[0]}/members", headers=headers, params={"limit": 1}
    )
    assert resp.status_code == 200
    # only one member: no next page -> mint one from the org list instead
    other = await async_client.get(
        f"/api/v1/orgs/{orgs[1]}/members",
        headers=headers,
        params={"limit": 1, "cursor": cursor},
    )
    assert other.status_code == 400
