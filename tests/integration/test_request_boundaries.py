"""Request-boundary mappings of Public API 1.0: a ``MissingTenantError`` is
the ``400`` of ``TenantScope()`` (G-TENANT-12, gap closed), and
``current_service`` is a service authenticator (spec §2.3)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

import sandloom.core.http.tenant_scope as _tenant_scope
from sandloom.core import di
from sandloom.core.auth.tokens import create_access_token
from sandloom.core.config.settings import settings
from sandloom.core.http.error_handlers import register_error_handlers
from sandloom.core.route_policy import resolve_route_policy
from sandloom.core.tenant_context import MissingTenantError
from sandloom.shared.guards.service import current_service

pytestmark = pytest.mark.asyncio


async def test_a_missing_tenant_at_the_boundary_is_the_tenant_scope_400() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/needs-tenant", dependencies=[di.public(reason="probe")])
    async def needs_tenant() -> None:
        raise MissingTenantError("query on TenantScoped model X with no tenant bound")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/needs-tenant")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "tenant context required"
    assert "TenantScoped" not in resp.text


async def test_current_service_authenticates_a_service_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "internal_api_token", "s3cr3t-token")
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/internal")
    async def internal(service: Any = Depends(current_service)) -> dict[str, Any]:
        return {"roles": sorted(service.roles), "id": str(service.user_id)}

    (route,) = [r for r in app.routes if getattr(r, "path", "") == "/internal"]
    assert resolve_route_policy(route).auth == "service"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/internal")).status_code == 404
        bad = await c.get("/internal", headers={"X-Internal-Token": "nope"})
        assert bad.status_code == 404
        ok = await c.get("/internal", headers={"X-Internal-Token": "s3cr3t-token"})
    assert ok.status_code == 200
    assert ok.json() == {
        "roles": ["service"],
        "id": "00000000-0000-0000-0000-000000000000",
    }


def _tenant_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/scoped", dependencies=[di.public(reason="probe")])
    async def scoped(tenant: Any = di.TenantScope()) -> dict[str, str]:
        return {"tenant": str(tenant)}

    return app


async def test_tenant_scope_without_a_tenant_answers_400() -> None:
    """``TenantScope()``'s own refusal (G-TENANT-12): no selector, no claim."""
    async with AsyncClient(
        transport=ASGITransport(app=_tenant_app()), base_url="http://t"
    ) as c:
        resp = await c.get("/scoped")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "tenant context required"


async def test_a_token_granting_several_tenants_needs_the_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_tenant`` (G-TENANT-12): several granted tenants and no
    ``X-Tenant-Id`` is a 400 naming the header; the header picks one."""

    async def _not_revoked(_request: Any, _claims: Any) -> None:
        return None  # the subject record is not what this test is about

    monkeypatch.setattr(_tenant_scope, "ensure_token_not_revoked", _not_revoked)
    one, two = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(
        {
            "sub": str(uuid.uuid4()),
            "token_type": "access",
            "tenant_ids": [str(one), str(two)],
        }
    )
    auth = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=_tenant_app()), base_url="http://t"
    ) as c:
        resp = await c.get("/scoped", headers=auth)
        assert resp.status_code == 400
        assert "X-Tenant-Id is required" in resp.json()["detail"]
        picked = await c.get("/scoped", headers={**auth, "X-Tenant-Id": str(two)})
    assert picked.status_code == 200
    assert picked.json() == {"tenant": str(two)}
