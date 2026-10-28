"""Projects API tests."""

import uuid

import pytest
from httpx import AsyncClient

from sandloom.sdk.testing import as_user, published

from ..events import ProjectCreated, ProjectUpdated


@pytest.mark.asyncio
class TestProjects:
    async def test_cannot_select_foreign_tenant_or_update_its_project(
        self, client: AsyncClient
    ):
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        _, alice = as_user(tenant_id=mine)
        _, bob = as_user(tenant_id=theirs)
        rejected = await client.post(
            "/api/v1/projects",
            json={"name": "foreign", "tenant_id": str(theirs)},
            headers=alice,
        )
        assert rejected.status_code == 422
        created = await client.post(
            "/api/v1/projects", json={"name": "owned"}, headers=bob
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == str(theirs)
        changed = await client.patch(
            f"/api/v1/projects/{created.json()['id']}",
            json={"name": "stolen"},
            headers=alice,
        )
        assert changed.status_code == 404

    async def test_create_and_update(self, client: AsyncClient):
        _, headers = as_user()

        created = await client.post(
            "/api/v1/projects",
            json={"name": "Dev Platform"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "Dev Platform"
        assert body["version"] == 1
        assert [e.version for e in published(ProjectCreated)] == [1]

        updated = await client.patch(
            f"/api/v1/projects/{body['id']}",
            json={"status": "archived"},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "archived"
        assert updated.json()["version"] == 2
        [event] = published(ProjectUpdated)
        assert (event.status, event.version) == ("archived", 2)

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/projects",
            json={"name": "X"},
        )
        assert resp.status_code == 401
