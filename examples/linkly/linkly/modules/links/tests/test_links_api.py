"""Links API tests — public shortener flow, cache-backed click counting."""

import uuid

import pytest
from httpx import AsyncClient

from sandloom.sdk.testing import published

from ..events import LinkCreated


def _code() -> str:
    return "t" + uuid.uuid4().hex[:8]


@pytest.mark.asyncio
class TestLinks:
    async def test_create_resolve_and_count(self, client: AsyncClient) -> None:
        code = _code()
        created = await client.post(
            "/api/v1/links",
            json={"target_url": "https://example.com/page", "custom_code": code},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["code"] == code
        assert body["clicks"] == 0
        assert [e.code for e in published(LinkCreated)] == [code]

        # Resolve -> 307 redirect to the target (no auth required).
        r1 = await client.get(f"/api/v1/links/{code}", follow_redirects=False)
        assert r1.status_code == 307
        assert r1.headers["location"] == "https://example.com/page"

        # Second resolve is served from the cache and bumps the counter again.
        await client.get(f"/api/v1/links/{code}", follow_redirects=False)

        stats = await client.get(f"/api/v1/links/{code}/stats")
        assert stats.status_code == 200
        assert stats.json()["clicks"] == 2

    async def test_resolve_unknown_is_404(self, client: AsyncClient) -> None:
        resp = await client.get(f"/api/v1/links/{_code()}", follow_redirects=False)
        assert resp.status_code == 404

    async def test_rejects_non_http_url(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/links", json={"target_url": "javascript:alert(1)"}
        )
        assert resp.status_code == 422

    async def test_duplicate_custom_code_conflicts(self, client: AsyncClient) -> None:
        code = _code()
        first = await client.post(
            "/api/v1/links",
            json={"target_url": "https://a.example", "custom_code": code},
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/v1/links",
            json={"target_url": "https://b.example", "custom_code": code},
        )
        assert second.status_code == 409
        assert len(published(LinkCreated)) == 1  # the refused one published nothing
