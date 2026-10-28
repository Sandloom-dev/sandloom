"""Organization CRUD and authorization tests."""

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestOrganizationCRUD:
    async def test_create_organization_makes_caller_owner(
        self, async_client: AsyncClient, user_a
    ):
        uid, headers = user_a
        slug = f"org-{uuid.uuid4().hex[:8]}"
        resp = await async_client.post(
            "/api/v1/orgs", json={"name": "My Org", "slug": slug}, headers=headers
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "My Org"
        assert body["slug"] == slug
        assert body["owner_id"] == str(uid)
        assert body["is_active"] is True

        # Creator appears as owner in members list
        members = await async_client.get(
            f"/api/v1/orgs/{body['id']}/members", headers=headers
        )
        assert members.status_code == 200
        assert [m["role"] for m in members.json()] == ["owner"]

    async def test_create_requires_auth(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/orgs", json={"name": "X", "slug": "no-auth-org"}
        )
        assert resp.status_code == 401

    async def test_duplicate_slug_conflict(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/orgs",
            json={"name": "Another", "slug": org_with_owner["slug"]},
            headers=headers,
        )
        assert resp.status_code == 409

    async def test_invalid_slug_rejected(self, async_client: AsyncClient, user_a):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/orgs",
            json={"name": "Bad Slug", "slug": "Bad Slug!"},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_list_my_organizations(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        _, headers_a = user_a
        _, headers_b = user_b
        mine = await async_client.get("/api/v1/orgs", headers=headers_a)
        assert mine.status_code == 200
        assert org_with_owner["id"] in [o["id"] for o in mine.json()]

        theirs = await async_client.get("/api/v1/orgs", headers=headers_b)
        assert theirs.status_code == 200
        assert org_with_owner["id"] not in [o["id"] for o in theirs.json()]

    async def test_non_member_gets_404(
        self, async_client: AsyncClient, user_b, org_with_owner
    ):
        _, headers_b = user_b
        resp = await async_client.get(
            f"/api/v1/orgs/{org_with_owner['id']}", headers=headers_b
        )
        assert resp.status_code == 404

    async def test_update_organization_by_owner(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers = user_a
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_with_owner['id']}",
            json={"name": "Renamed"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    async def test_soft_delete_by_owner_hides_org(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers = user_a
        resp = await async_client.delete(
            f"/api/v1/orgs/{org_with_owner['id']}", headers=headers
        )
        assert resp.status_code == 204
        gone = await async_client.get(
            f"/api/v1/orgs/{org_with_owner['id']}", headers=headers
        )
        # Org is inactive: membership still resolves, entity returns 404
        assert gone.status_code == 404
