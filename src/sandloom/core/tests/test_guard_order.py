"""A-03: ``create_app`` refuses an idempotency guard solved before authentication."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from sandloom.core.cli.generated import render_stub
from sandloom.core.di import authenticates, idempotency_guard
from sandloom.core.guard_order import (
    GuardOrderError,
    guard_order_problems,
    resolution_order,
    validate_guard_order,
)
from sandloom.sdk import contract
from sandloom.shared.authz import (
    Account,
    Category,
    From,
    Role,
    access_check,
    require,
)
from sandloom.shared.guards.auth import get_current_user_id


def _routes(router: APIRouter) -> list[Any]:
    app = FastAPI()
    app.include_router(router)
    return list(app.routes)


def test_guard_in_route_dependencies_before_param_auth_is_refused() -> None:
    router = APIRouter()

    @router.post("/x", dependencies=[Depends(idempotency_guard(scope="x"))])
    async def x(user_id: UUID = Depends(get_current_user_id)) -> None: ...

    problems = guard_order_problems(_routes(router))
    assert len(problems) == 1 and "POST /x" in problems[0]
    with pytest.raises(GuardOrderError, match="POST /x"):
        validate_guard_order(_routes(router))


def test_guard_param_before_auth_param_is_refused() -> None:
    router = APIRouter()

    @router.post("/x")
    async def x(
        _key: str | None = Depends(idempotency_guard(scope="x")),
        user_id: UUID = Depends(get_current_user_id),
    ) -> None: ...

    assert guard_order_problems(_routes(router))


def test_guard_after_auth_param_passes() -> None:
    router = APIRouter()

    @router.post("/x")
    async def x(
        user_id: UUID = Depends(get_current_user_id),
        _key: str | None = Depends(idempotency_guard(scope="x")),
    ) -> None: ...

    assert guard_order_problems(_routes(router)) == []


def test_router_level_auth_precedes_route_guard() -> None:
    router = APIRouter(dependencies=[Depends(require(Role("member")))])

    @router.post("/x", dependencies=[Depends(idempotency_guard(scope="x"))])
    async def x() -> None: ...

    assert guard_order_problems(_routes(router)) == []


def test_anonymous_route_with_a_guard_is_not_reported() -> None:
    router = APIRouter()

    @router.post("/x", dependencies=[Depends(idempotency_guard(scope="x"))])
    async def x() -> None: ...

    assert guard_order_problems(_routes(router)) == []


def test_custom_authenticator_counts_once_marked() -> None:
    async def api_key(request: Request) -> str:
        return "k"

    router = APIRouter()

    @router.post("/x", dependencies=[Depends(idempotency_guard(scope="x"))])
    async def x(_: str = Depends(api_key)) -> None: ...

    assert guard_order_problems(_routes(router)) == []  # unknown: not auth
    authenticates(api_key)
    assert guard_order_problems(_routes(router))


async def test_resolution_order_is_fastapi_depth_first_and_cached() -> None:
    calls: list[str] = []

    async def a() -> None:
        calls.append("a")

    async def b(_: None = Depends(a)) -> None:
        calls.append("b")

    async def c() -> None:
        calls.append("c")

    router = APIRouter(dependencies=[Depends(c)])

    @router.get("/x", dependencies=[Depends(b)])
    async def x(_a: None = Depends(a), _c: None = Depends(c)) -> None: ...

    (route,) = [r for r in _routes(router) if getattr(r, "path", "") == "/x"]
    names = [getattr(f, "__name__", "") for f in resolution_order(route.dependant)]
    assert names == ["c", "a", "b"]

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/x")
    assert calls == names  # the order FastAPI actually ran them in


# --- SANDLOOM-B007: an org-scoped gate must not run before TenantScope() ---------


PLANS = "fixture.plans.v1"


@contract(PLANS)
class PlansContract:
    """Subscriptions (a fixture provider, declared as billing declares it)."""

    @access_check(
        "SubscriptionActive",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="active subscription",
    )
    async def check_subscription_active(
        self, tenant_type: str, tenant_id: UUID
    ) -> bool:
        """True while a subscription grants access."""
        return True


@pytest.fixture
def plans(tmp_path: Path) -> Iterator[Any]:
    """The generated atoms of :data:`PLANS`, as ``sandloom generate`` renders
    them beside a consumer's stub."""
    path = tmp_path / "fixture_plans_v1.py"
    path.write_text(render_stub(PLANS, PlansContract))
    spec = importlib.util.spec_from_file_location("_guard_order_plans_v1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_org_gate_before_tenant_scope_is_refused(plans: Any) -> None:
    """Agent A's trap: the gate in decorator dependencies runs first."""
    from sandloom.core.di import TenantScope
    from sandloom.core.guard_order import (
        GateOrderError,
        gate_order_problems,
        validate_gate_order,
    )

    def org_gate() -> Any:
        return require(plans.SubscriptionActive(of=Account.org()))

    router = APIRouter()

    @router.post("/notes", dependencies=[Depends(org_gate())])
    async def trap(tenant: UUID = TenantScope()) -> None: ...

    @router.post("/orgs/{org_id}/notes", dependencies=[Depends(org_gate())])
    async def from_path(org_id: UUID, tenant: UUID = TenantScope()) -> None: ...

    @router.post("/ok")
    async def ok(
        tenant: UUID = TenantScope(), _ctx: Any = Depends(org_gate())
    ) -> None: ...

    @router.post(
        "/personal",
        dependencies=[Depends(require(plans.SubscriptionActive(of=Account.user())))],
    )
    async def personal(tenant: UUID = TenantScope()) -> None: ...

    problems = gate_order_problems(_routes(router))
    assert len(problems) == 1 and problems[0].startswith("POST /notes:")
    with pytest.raises(GateOrderError, match="SANDLOOM-B007"):
        validate_gate_order(_routes(router))
