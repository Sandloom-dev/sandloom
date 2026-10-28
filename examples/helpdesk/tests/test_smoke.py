"""Smoke test: the application boots and wires its modules.

Touches no database — it only proves the ASGI app assembles, the framework
ops endpoints respond, and the sample module's router is mounted behind auth.
"""

import httpx
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_app_boots_and_health_ok() -> None:
    from helpdesk.asgi import app

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/simple")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_tickets_router_mounted() -> None:
    import uuid

    from helpdesk.asgi import app

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # The org-scoped create endpoint exists and enforces auth (401),
        # which proves the tickets module registered and its router is wired in.
        resp = await client.post(
            f"/api/v1/orgs/{uuid.uuid4()}/tickets", json={"subject": "x"}
        )
        assert resp.status_code == 401


def test_every_route_declares_its_operation_class() -> None:
    """ADR-0009 (the operation classes are gone): every route is authenticated
    or public with a reason; create_app refuses anything else (SANDLOOM-B008)."""
    from helpdesk.asgi import app
    from sandloom.sdk.testing import route_policy  # sandloom: advanced route policy

    routes = [
        r
        for r in app.routes
        if getattr(r, "dependant", None) is not None and getattr(r, "methods", None)
    ]
    assert routes
    for route in routes:
        policy = route_policy(route)
        assert policy.auth != "public" or policy.public_reason, route.path
