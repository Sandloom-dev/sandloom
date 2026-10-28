"""Tasks API tests."""

import uuid

import pytest
from httpx import AsyncClient

from sandloom.sdk.testing import as_user


@pytest.mark.asyncio
class TestTasks:
    async def test_task_mutation_is_scoped_to_signed_tenant(self, client):
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        _, alice = as_user(tenant_id=mine)
        _, bob = as_user(tenant_id=theirs)
        created = await client.post(
            "/api/v1/tasks",
            json={"project_id": str(uuid.uuid4()), "title": "theirs"},
            headers=bob,
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == str(theirs)
        changed = await client.patch(
            f"/api/v1/tasks/{created.json()['id']}",
            json={"title": "stolen"},
            headers=alice,
        )
        assert changed.status_code == 404

    async def test_create_task(self, client: AsyncClient):
        _, headers = as_user()
        created = await client.post(
            "/api/v1/tasks",
            json={
                "project_id": str(uuid.uuid4()),
                "title": "Wire the projection",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["version"] == 1

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/tasks",
            json={
                "project_id": str(uuid.uuid4()),
                "title": "X",
            },
        )
        assert resp.status_code == 401
