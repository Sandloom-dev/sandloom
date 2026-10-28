"""Smoke test: the application boots and wires its modules.

Touches no database — it only proves the ASGI app assembles, the framework
ops endpoints respond, and the sample module's router is mounted behind auth.
"""

import httpx
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_app_boots_and_health_ok() -> None:
    from tasklist.asgi import app

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/simple")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_sample_module_router_mounted() -> None:
    from tasklist.asgi import app

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # The sample module's create endpoint exists and authenticates (401),
        # which proves the module was discovered and its router mounted.
        resp = await client.post("/api/v1/tasks/items", json={"title": "x"})
        assert resp.status_code == 401
