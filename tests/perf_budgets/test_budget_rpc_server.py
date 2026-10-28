"""Budget: an internal RPC call runs no FastAPI dependency resolution.

The RPC routes are plain Starlette ``Route``s, and in an app wired by
``install_service_rpc_routes`` the outermost ``RpcFastPathMiddleware`` serves
``/_sandloom/rpc/*`` before the HTTP middleware stack (CORS, request logging,
idempotency capture, routing). Auth (the pair MAC), the body-size cap and
the error envelope still apply — asserted here too.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import msgpack
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Route

from sandloom.core import contract_wire
from sandloom.core.contract_testing import rpc_request_headers, rpc_settings
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import install_service_rpc_routes, rpc_router


class Echo:
    @classmethod
    @contract_method
    async def pair(cls, org: UUID, user: UUID) -> list[UUID]:
        return [org, user]


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    session: _FakeSession | None = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = _FakeSession()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from sandloom.core.config.settings import settings

    for name, value in rpc_settings(
        "svc", allowed_calls={"svc": ["demo.budget.v1"]}
    ).items():
        monkeypatch.setattr(settings, name, value)
    application = FastAPI()
    core = CoreFacade(app=application)
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core.register_service("demo.budget.v1", Echo)
    application.state.core = core

    inner_calls: list[str] = []

    @application.middleware("http")
    async def inner(request: Any, call_next: Any) -> Any:  # the "heavy" stack
        inner_calls.append(request.url.path)
        return await call_next(request)

    @application.get("/plain")
    async def plain() -> dict[str, bool]:
        return {"ok": True}

    install_service_rpc_routes(application, core)
    application.state.inner_calls = inner_calls
    return application


def test_rpc_routes_are_plain_starlette_routes() -> None:
    routes = rpc_router(CoreFacade(app=FastAPI())).routes
    assert routes and all(type(r) is Route for r in routes)
    assert not any(isinstance(r, APIRoute) for r in routes)


@pytest.mark.asyncio
async def test_rpc_call_skips_dependency_resolution_and_middleware(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fastapi.routing

    solved: list[int] = []
    original = fastapi.routing.solve_dependencies

    async def counting(*args: Any, **kwargs: Any) -> Any:
        solved.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "solve_dependencies", counting)
    from sandloom.core.config.settings import settings

    org, user = uuid4(), uuid4()
    body = msgpack.packb(
        {"args": [contract_wire.encode(org), contract_wire.encode(user)], "kwargs": {}}
    )
    call_auth = rpc_request_headers(settings, "demo.budget.v1/pair", body)
    describe_auth = rpc_request_headers(settings, "demo.budget.v1", http_method="GET")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        ok = await client.post(
            "/_sandloom/rpc/demo.budget.v1/pair",
            content=body,
            headers={**call_auth, "X-Request-ID": "rid-1"},
        )
        hidden = await client.post("/_sandloom/rpc/demo.budget.v1/pair", content=body)
        described = await client.get(
            "/_sandloom/rpc/demo.budget.v1", headers=describe_auth
        )
    # BUDGET: zero FastAPI dependency resolutions, zero inner-middleware hops.
    assert solved == []
    assert app.state.inner_calls == []
    assert ok.status_code == 200 and ok.headers["X-Request-ID"] == "rid-1"
    assert contract_wire.decode(msgpack.unpackb(ok.content)["result"]) == [org, user]
    assert hidden.status_code == 404  # authentication still enforced
    assert described.status_code == 200
    # Control: an ordinary FastAPI route does resolve dependencies (the
    # counter works) and goes through the middleware stack.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/plain")).status_code == 200
    assert solved == [1] and app.state.inner_calls == ["/plain"]


@pytest.mark.asyncio
async def test_fast_path_enforces_the_body_cap(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "max_request_body_bytes", 64)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/_sandloom/rpc/demo.budget.v1/pair",
            content=b"x" * 1024,
            headers=rpc_request_headers(settings, "demo.budget.v1/pair", b"x" * 1024),
        )
    assert resp.status_code == 413
