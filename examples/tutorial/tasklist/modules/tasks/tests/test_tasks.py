"""Tests of the tasks module (fixtures: the sandloom.sdk.testing plugin)."""

from typing import Any
from uuid import uuid4

from sandloom.sdk.testing import as_user, fake_contract, published
from tasklist.shared.contracts.stats_quota_v1 import STATS_QUOTA_V1

from ..events import ItemCreated


class Quota:
    """A test double of ``stats.quota.v1``."""

    def __init__(self, allow: bool) -> None:
        self.allow = allow

    async def may_create_item(self) -> bool:
        return self.allow


# --8<-- [start:tests]
async def test_create_and_read_an_item(client: Any) -> None:
    _, headers = as_user(tenant_id=uuid4())
    with fake_contract(STATS_QUOTA_V1, Quota(allow=True)):
        created = await client.post(
            "/api/v1/tasks/items", json={"title": "first"}, headers=headers
        )
    assert created.status_code == 201, created.text
    assert len(published(ItemCreated)) == 1
    item = created.json()
    fetched = await client.get(f"/api/v1/tasks/items/{item['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "first"


async def test_the_quota_refuses_a_new_item(client: Any) -> None:
    _, headers = as_user(tenant_id=uuid4())
    with fake_contract(STATS_QUOTA_V1, Quota(allow=False)):
        response = await client.post(
            "/api/v1/tasks/items", json={"title": "x"}, headers=headers
        )
    assert response.status_code == 402
    assert published(ItemCreated) == []


async def test_items_need_an_authenticated_user(client: Any) -> None:
    response = await client.post("/api/v1/tasks/items", json={"title": "x"})
    assert response.status_code == 401


# --8<-- [end:tests]
