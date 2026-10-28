"""``module_router()`` (spec §5.1; ledger C-DECL-03).

Per route, at decoration time: user authentication and the tenant binding,
never on ``public()`` or service routes; no router-level auth.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI

from sandloom.sdk import CurrentUser, module_router, public
from sandloom.sdk._decl.web import ModuleRouter
from sandloom.sdk.authz import current_context
from sandloom.sdk.errors import RoutePolicyError
from sandloom.sdk.web import CurrentService, TenantId, authenticates, idempotency_guard


def _deps(router: ModuleRouter, path: str) -> list[str]:
    route = next(r for r in router.routes if getattr(r, "path", None) == path)
    out = []
    for dep in route.dependencies:
        call = dep.dependency
        if getattr(call, "__sandloom_binds_tenant__", False):
            out.append("tenant")
        elif call is current_context:
            out.append("auth")
        else:
            out.append(getattr(call, "__name__", repr(call)))
    return out


def test_prefix_and_tags_are_derived_from_the_module() -> None:
    router = module_router()
    # Built in this test module (tests/sdk): the parent package's name.
    assert router.prefix.startswith("/api/v1/")
    assert router.tags == [router.prefix.rsplit("/", 1)[-1]]
    explicit = module_router(prefix="/x", tags=["t"])
    assert (explicit.prefix, explicit.tags) == ("/x", ["t"])
    assert router.dependencies == []  # never router-level auth


def test_authentication_and_tenant_are_injected_per_route() -> None:
    router = module_router(tenant="required")

    @router.get("/a")
    async def a(user: CurrentUser) -> None: ...

    @router.post("/b", dependencies=[Depends(idempotency_guard(scope="m.b"))])
    async def b() -> None: ...

    @router.get("/c", tenant="optional")
    async def c() -> None: ...

    @router.get("/d", tenant="none")
    async def d() -> None: ...

    @router.get("/e")
    async def e(tenant: TenantId) -> None: ...

    prefix = router.prefix
    assert _deps(router, f"{prefix}/a") == ["auth", "tenant"]
    # injected in front of the route's own dependencies (guard order, SANDLOOM-B002)
    assert _deps(router, f"{prefix}/b")[:2] == ["auth", "tenant"]
    assert _deps(router, f"{prefix}/c") == ["auth", "tenant"]
    assert _deps(router, f"{prefix}/d") == ["auth"]
    # the route binds the tenant itself: no second binder
    assert _deps(router, f"{prefix}/e") == ["auth"]


def test_optional_binding_does_not_require_a_tenant() -> None:
    router = module_router(tenant="none")

    @router.get("/o", tenant="optional")
    async def o() -> None: ...

    route = router.routes[0]
    binder = route.dependencies[1].dependency
    assert binder.__sandloom_binds_tenant__  # type: ignore[attr-defined]
    assert binder.__closure__ is not None
    cells = [c.cell_contents for c in binder.__closure__]
    assert [x for x in cells if isinstance(x, bool)] == [False]  # required=False


def test_public_and_service_routes_get_nothing() -> None:
    router = module_router(tenant="required")

    @router.post("/login", dependencies=[public(reason="credential exchange")])
    async def login() -> None: ...

    @router.post("/internal")
    async def internal(service: CurrentService) -> None: ...

    @authenticates(kind="service")
    async def metrics_token() -> None: ...

    @router.get("/metrics", dependencies=[Depends(metrics_token)])
    async def metrics() -> None: ...

    assert _deps(router, f"{router.prefix}/login") == ["public"]
    assert _deps(router, f"{router.prefix}/internal") == []
    assert _deps(router, f"{router.prefix}/metrics") == ["metrics_token"]


def test_public_with_an_authenticator_is_refused() -> None:
    router = module_router()
    with pytest.raises(RoutePolicyError, match="SANDLOOM-B008"):

        @router.get("/both", dependencies=[public(reason="r")])
        async def both(user: CurrentUser) -> None: ...

    with pytest.raises(RuntimeError, match="needs a reason.*SANDLOOM-B008"):
        public(reason="  ")


def test_tenant_binding_is_derived_from_tenant_scoped_models() -> None:
    import sdkfix_notes.api as notes_api
    import sdkfix_stats.consumers  # noqa: F401 - a module without TenantScoped models

    assert notes_api.router._derived_binding() == "required"
    plain = ModuleRouter(sandloom_owner="sdkfix_stats.api", prefix="/s")
    assert plain._derived_binding() == "none"


async def test_unauthenticated_requests_are_refused_and_public_ones_served() -> None:
    router = module_router(prefix="/r", tenant="none")

    @router.get("/private")
    async def private() -> dict[str, Any]:
        return {"ok": True}

    @router.get("/open", dependencies=[public(reason="probe")])
    async def open_() -> dict[str, Any]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        assert (await http.get("/r/private")).status_code == 401
        assert (await http.get("/r/open")).json() == {"ok": True}
