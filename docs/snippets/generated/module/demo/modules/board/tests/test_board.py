"""Tests of the board module (sandloom.sdk.testing fixtures)."""

from typing import Any
from uuid import uuid4

from sandloom.sdk.testing import as_user, published

from ..events import ItemCreated


async def test_create_and_read_an_item(client: Any) -> None:
    _, headers = as_user(tenant_id=uuid4())
    created = await client.post(
        "/api/v1/board/items", json={"title": "first"}, headers=headers
    )
    assert created.status_code == 201, created.text
    assert len(published(ItemCreated)) == 1
    item = created.json()
    fetched = await client.get(f"/api/v1/board/items/{item['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "first"


async def test_list_items_is_capped_and_tenant_scoped(client: Any) -> None:
    _, headers = as_user(tenant_id=uuid4())
    _, other = as_user(tenant_id=uuid4())
    for title in ("a", "b", "c"):
        created = await client.post(
            "/api/v1/board/items", json={"title": title}, headers=headers
        )
        assert created.status_code == 201, created.text
    listed = await client.get("/api/v1/board/items", headers=headers)
    assert listed.status_code == 200
    # Newest first, deterministically: all three rows share one transaction.
    assert [item["title"] for item in listed.json()] == ["c", "b", "a"]
    page = await client.get("/api/v1/board/items", params={"limit": 2}, headers=headers)
    assert len(page.json()) == 2
    foreign = await client.get("/api/v1/board/items", headers=other)
    assert foreign.json() == []
    over_cap = await client.get(
        "/api/v1/board/items", params={"limit": 101}, headers=headers
    )
    assert over_cap.status_code == 422


async def test_items_need_an_authenticated_user(client: Any) -> None:
    response = await client.post("/api/v1/board/items", json={"title": "x"})
    assert response.status_code == 401
