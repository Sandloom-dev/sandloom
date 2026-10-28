"""Route markers are pruned from the executed dependencies (sandloom.core.route_markers).

Route markers (``public``, ``sensitive``, ``no_idempotency``, ...) are no-op
declarations (ADR-0009). ``create_app``
prunes them from ``route.dependant.dependencies`` so FastAPI stops solving
them per request. These tests pin that the pruning changes NOTHING observable:

* the pruned markers are really no longer called (the FastAPI-version guard:
  if a FastAPI release stopped honouring the in-place pruning this fails);
* every reader (the resolved route policy, validations) answers the same before and
  after; ``route.dependencies`` keeps the declaration;
* OpenAPI is identical;
* differential: identical status / headers / body for fresh, revoked, blocked
  and missing tokens, pruned vs not (real Redis);
* ``dependency_overrides``, mounted sub-apps, routes added after the pruning
  and websocket routes keep working; outside the verified FastAPI range
  nothing is pruned (warning).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
)
from fastapi.routing import APIRoute
from fastapi.security import OAuth2PasswordBearer

from sandloom.core import route_markers
from sandloom.core.auth import marks, revocation, tokens
from sandloom.core.config.settings import settings
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.infra import access
from sandloom.core.route_markers import (
    is_marker,
    prune_marker_dependencies,
    register_marker,
    route_marker_calls,
)
from sandloom.core.route_policy import (
    NO_IDEMPOTENCY_ATTR,
    PUBLIC_ATTR,
    REPLAY_SAFE_ATTR,
    SENSITIVE_ATTR,
    public,
    replay_safe,
    resolve_route_policy,
    route_policy_problems,
    sensitive,
)
from sandloom.core.testing import reload_test_env

#: The marker attribute a test marker carries (a sensitive() declaration).
CLASS_ATTR = SENSITIVE_ATTR
MARKER_ATTRS = (PUBLIC_ATTR, NO_IDEMPOTENCY_ATTR, REPLAY_SAFE_ATTR, SENSITIVE_ATTR)


def operation_class_map(routes: Any) -> dict[tuple[str, str], Any]:
    """``{(method, path): resolved policy}`` of every API route (the reader)."""
    from sandloom.core.route_policy import api_routes

    return {
        (method, str(route.path)): resolve_route_policy(route, method)
        for route in api_routes(routes)
        for method in sorted(route.methods)
    }


def operation_class_problems(routes: Any) -> list[str]:
    return route_policy_problems(routes)


BACKEND = Path(__file__).resolve().parents[4]
ENV_TEST = BACKEND / ".env.test"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    if ENV_TEST.exists():
        reload_test_env(ENV_TEST)


def _counting_marker(counter: list[int], name: str = "admin") -> Any:
    """A marker (same shape as sensitive()'s) that counts its calls."""

    async def _marker() -> None:
        counter.append(1)

    setattr(_marker, CLASS_ATTR, name)
    return register_marker(_marker)


def _executed_markers(app: FastAPI) -> list[Any]:
    out: list[Any] = []

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            if is_marker(sub.call):
                out.append(sub.call)
            walk(sub)

    for route in route_markers.iter_routes(app.routes):
        if isinstance(route, APIRoute):
            walk(route.dependant)
    return out


def _declared(route: Any) -> list[tuple[str, Any]]:
    return sorted(
        (attr, getattr(call, attr))
        for call in route_marker_calls(route)
        for attr in MARKER_ATTRS
        if call is not None and getattr(call, attr, None) is not None
    )


# --------------------------------------------------------------------------- #
# probe app: every marker shape, router- and route-level
# --------------------------------------------------------------------------- #
def _probe_app() -> FastAPI:
    from sandloom.shared.guards.auth import get_current_user_id

    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[sensitive("admin")])

    @admin.get("/report")
    async def _report(uid: UUID = Depends(get_current_user_id)) -> dict[str, str]:
        return {"uid": str(uid)}

    @admin.post("/act")
    async def _act(
        payload: dict[str, Any] = Body(...),
        uid: UUID = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        return {"uid": str(uid), "payload": payload}

    app.include_router(admin)

    @app.get("/plain")
    async def _plain(uid: UUID = Depends(get_current_user_id)) -> dict[str, str]:
        return {"uid": str(uid)}

    @app.get("/read", dependencies=[replay_safe(reason="probe")])
    async def _read(
        uid: UUID = Depends(get_current_user_id),
        x_probe: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return {"uid": str(uid), "probe": x_probe}

    @app.get("/public", dependencies=[public(reason="probe")])
    async def _public() -> dict[str, str]:
        return {"ok": "yes"}

    return app


#: Markers in the probe app: /admin/* (2 routes x 1), /read, /public.
PROBE_MARKERS = 4


def _pruned_probe() -> FastAPI:
    app = _probe_app()
    assert prune_marker_dependencies(app.routes) == PROBE_MARKERS
    return app


# --------------------------------------------------------------------------- #
# pruning itself
# --------------------------------------------------------------------------- #
async def test_pruned_markers_are_no_longer_called() -> None:
    """The FastAPI-version guard: the handler honours the pruned dependant."""
    calls: list[int] = []
    marker = _counting_marker(calls)

    def build() -> FastAPI:
        app = FastAPI()
        router = APIRouter(dependencies=[Depends(marker)])

        @router.get("/x", dependencies=[Depends(marker)])
        async def _x() -> dict[str, int]:
            return {"ok": 1}

        app.include_router(router)
        return app

    plain = build()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plain), base_url="http://t"
    ) as client:
        assert (await client.get("/x")).status_code == 200
    # FastAPI caches one call per request: executed once while declared
    assert len(calls) == 1

    pruned = build()
    assert prune_marker_dependencies(pruned.routes) == 2
    calls.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pruned), base_url="http://t"
    ) as client:
        for _ in range(3):
            assert (await client.get("/x")).json() == {"ok": 1}
    assert calls == []


def test_readers_answer_identically_and_declaration_is_kept() -> None:
    plain, pruned = _probe_app(), _pruned_probe()
    assert _executed_markers(plain) and not _executed_markers(pruned)
    assert operation_class_map(pruned.routes) == operation_class_map(plain.routes)
    assert operation_class_problems(pruned.routes) == operation_class_problems(
        plain.routes
    )
    for a, b in zip(plain.routes, pruned.routes, strict=True):
        if isinstance(a, APIRoute):
            assert isinstance(b, APIRoute)
            # the declaration (what include_router copies) is untouched
            assert len(a.dependencies) == len(b.dependencies)
            assert _declared(a) == _declared(b)


def test_pruning_is_idempotent() -> None:
    app = _pruned_probe()
    before = operation_class_map(app.routes)
    assert prune_marker_dependencies(app.routes) == 0
    assert operation_class_map(app.routes) == before


def test_openapi_is_identical() -> None:
    oauth = OAuth2PasswordBearer(tokenUrl="/token", scopes={"read": "r"})

    def build() -> FastAPI:
        app = _probe_app()

        @app.post(
            "/secured/{item_id}",
            dependencies=[sensitive("money")],
        )
        async def _secured(
            item_id: int,
            q: str | None = None,
            body: dict[str, Any] = Body(...),
            token: str = Depends(oauth),
        ) -> dict[str, Any]:
            return {"id": item_id}

        return app

    plain, pruned = build(), build()
    assert prune_marker_dependencies(pruned.routes) == PROBE_MARKERS + 1
    assert pruned.openapi() == plain.openapi()


def test_route_added_after_pruning_is_read_identically() -> None:
    app = _pruned_probe()

    @app.get("/late", dependencies=[sensitive("security"), public(reason="x")])
    async def _late() -> dict[str, str]:
        return {}

    @app.get(
        "/late2",
        dependencies=[public(reason="one"), public(reason="two")],
    )
    async def _late2() -> dict[str, str]:
        return {}

    assert operation_class_map(app.routes)[("GET", "/late")].risk == {
        "security": ("sensitive()",)
    }
    problems = operation_class_problems(app.routes)
    assert any("/late2" in p and "different reasons" in p for p in problems)


async def test_mounted_sub_app_is_pruned_and_served() -> None:
    calls: list[int] = []
    sub = FastAPI()

    @sub.get("/y", dependencies=[Depends(_counting_marker(calls))])
    async def _y() -> dict[str, int]:
        return {"y": 1}

    app = FastAPI()
    app.mount("/sub", sub)
    assert prune_marker_dependencies(app.routes) == 1
    assert operation_class_map(sub.routes)[("GET", "/y")].risk == {
        "admin": ("sensitive()",)
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/sub/y")).json() == {"y": 1}
    assert calls == []


def test_websocket_routes_are_left_alone() -> None:
    app = FastAPI()

    @app.websocket("/ws", dependencies=[sensitive("admin")])
    async def _ws(websocket: WebSocket) -> None:  # pragma: no cover - not served
        await websocket.close()

    assert prune_marker_dependencies(app.routes) == 0
    assert any(is_marker(c) for c in route_marker_calls(app.routes[-1]))


async def test_marker_bound_to_a_parameter_is_kept() -> None:
    calls: list[int] = []
    marker = _counting_marker(calls)

    async def guard(_m: None = Depends(marker)) -> str:
        return "g"

    app = FastAPI()

    @app.get("/g", dependencies=[public(reason="probe")])
    async def _g(g: str = Depends(guard)) -> dict[str, str]:
        return {"g": g}

    assert prune_marker_dependencies(app.routes) == 1  # only the route-level one
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/g")).json() == {"g": "g"}
    assert calls == [1]
    assert {
        getattr(c, CLASS_ATTR, None) for c in route_marker_calls(app.routes[-1])
    } >= {"admin"}


def test_marker_with_parameters_is_not_pruned() -> None:
    async def _odd(x_trace: str | None = Header(default=None)) -> None:
        return None

    setattr(_odd, CLASS_ATTR, "admin")
    register_marker(_odd)
    app = FastAPI()

    @app.get("/z", dependencies=[Depends(_odd)])
    async def _z() -> dict[str, int]:
        return {}

    assert prune_marker_dependencies(app.routes) == 0


async def test_wrapped_real_guard_is_not_pruned_and_still_runs() -> None:
    """functools.wraps(marker) copies the __sandloom_* attributes: not a marker."""
    marker = sensitive("admin").dependency
    ran: list[int] = []

    @functools.wraps(marker)
    async def real_guard() -> None:
        ran.append(1)
        raise HTTPException(403, "denied by the real guard")

    assert getattr(real_guard, CLASS_ATTR, None) == "admin"
    assert not is_marker(real_guard)
    app = FastAPI()

    @app.get("/w", dependencies=[Depends(real_guard), public(reason="probe")])
    async def _w() -> dict[str, int]:
        return {"ok": 1}

    assert prune_marker_dependencies(app.routes) == 1  # only public()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/w")
    assert resp.status_code == 403 and ran == [1]


async def test_permissive_getattr_proxy_is_not_pruned() -> None:
    ran: list[int] = []

    class Proxy:
        def __getattr__(self, name: str) -> Any:
            # answers every marker attribute (and any non-dunder name); real
            # dunders stay absent so FastAPI can introspect the call
            if name.startswith("__sandloom_") or not name.startswith("__"):
                return "admin"
            raise AttributeError(name)

        async def __call__(self) -> None:
            ran.append(1)

    proxy = Proxy()
    assert getattr(proxy, CLASS_ATTR) == "admin" and not is_marker(proxy)
    app = FastAPI()

    @app.get("/p", dependencies=[Depends(proxy)])
    async def _p() -> dict[str, int]:
        return {"ok": 1}

    assert prune_marker_dependencies(app.routes) == 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/p")).status_code == 200
    assert ran == [1]


def test_outside_verified_fastapi_range_nothing_is_pruned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(route_markers, "VERIFIED_FASTAPI_RANGE", ((0, 1), (0, 2)))
    monkeypatch.setattr(route_markers, "_warned_unverified", False)
    app = _probe_app()
    with caplog.at_level(logging.WARNING, logger="sandloom.core.route_markers"):
        assert prune_marker_dependencies(app.routes) == 0
        assert prune_marker_dependencies(app.routes) == 0
    assert len(_executed_markers(app)) == PROBE_MARKERS
    warnings = [r for r in caplog.records if "outside the verified" in r.getMessage()]
    assert len(warnings) == 1


def test_create_app_prunes_every_marker() -> None:
    from sandloom.main import app

    top_level = [
        sub.call
        for route in app.routes
        if isinstance(route, APIRoute)
        for sub in route.dependant.dependencies
        if is_marker(sub.call)
    ]
    assert top_level == []
    # no marker is bound to a dependency's PARAMETER on the platform
    assert _executed_markers(app) == []
    assert operation_class_map(app.routes)  # still declared and readable


# --------------------------------------------------------------------------- #
# differential behaviour over real Redis
# --------------------------------------------------------------------------- #
def _redis_url() -> str:
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("no test CACHE_URL configured")
    return url


@pytest_asyncio.fixture
async def redis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RedisCacheProvider]:
    provider = RedisCacheProvider(
        name="markers", driver="redis", options={"url": _redis_url()}, shared=False
    )
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await provider.aclose()
        pytest.skip("test Redis not reachable")
    monkeypatch.setattr(access, "cache", lambda name="default": provider)
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", True)
    yield provider
    await provider.aclose()


def _auth(sub: str) -> dict[str, str]:
    token = tokens.create_access_token(data={"sub": sub, "token_type": "access"})
    return {"Authorization": f"Bearer {token}", "X-Probe": "p"}


REQUESTS: list[tuple[str, str]] = [
    ("GET", "/plain"),
    ("GET", "/admin/report"),
    ("POST", "/admin/act"),
    ("GET", "/read"),
    ("GET", "/public"),
]


async def _answers(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> list[tuple[str, int, list[tuple[str, str]], bytes]]:
    out = []
    for method, path in REQUESTS:
        resp = await client.request(
            method,
            path,
            headers=headers,
            json={"a": 1} if method == "POST" else None,
        )
        out.append((path, resp.status_code, list(resp.headers.items()), resp.content))
    return out


def _statuses(answers: list[Any]) -> dict[str, int]:
    return {path: status for path, status, _h, _b in answers}


async def test_differential_strict_budgeted_revoked_blocked(
    redis_cache: RedisCacheProvider,
) -> None:
    plain_app, pruned_app = _probe_app(), _pruned_probe()
    calls: list[int] = []
    plain = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plain_app), base_url="http://p"
    )
    pruned = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pruned_app), base_url="http://p"
    )
    async with plain, pruned:

        async def both(headers: dict[str, str]) -> dict[str, int]:
            a, b = await _answers(plain, headers), await _answers(pruned, headers)
            assert a == b
            return _statuses(a)

        # no token / garbage token
        assert set((await both({})).values()) == {401, 200}
        await both({"Authorization": "Bearer nope"})

        # fresh token: everything answers
        sub = str(uuid4())
        headers = _auth(sub)
        assert set((await both(headers)).values()) == {200}

        # another process revoked: every authenticated route sees it at once
        await redis_cache.set(
            marks.subject_key(sub),
            marks._format_mark(marks._seconds_to_us(time.time() + 1)),
            ttl_seconds=120,
        )
        stale = await both(headers)
        assert stale["/public"] == 200
        assert {stale[p] for p in stale if p != "/public"} == {401}

        # own revocation: immediate everywhere
        sub2 = str(uuid4())
        headers2 = _auth(sub2)
        await both(headers2)
        assert await revocation.revoke_user_tokens(
            sub2, valid_after=int(time.time()) + 1
        )
        revoked = await both(headers2)
        assert {revoked[p] for p in revoked if p != "/public"} == {401}

        # emergency block: read live on every route
        sub3 = str(uuid4())
        headers3 = _auth(sub3)
        await both(headers3)
        await redis_cache.set(
            marks.subject_key(sub3), marks._format_blocked(marks._seconds_to_us(1))
        )
        try:
            blocked = await both(headers3)
        finally:
            await redis_cache.delete(marks.subject_key(sub3))
        assert {blocked[p] for p in blocked if p != "/public"} == {401}
    assert calls == []


async def test_dependency_overrides_still_apply_on_pruned_routes() -> None:
    from sandloom.shared.guards.auth import get_current_user_id

    app = _pruned_probe()
    fixed = uuid4()

    async def _fake() -> UUID:
        return fixed

    app.dependency_overrides[get_current_user_id] = _fake
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        for path in ("/admin/report", "/read"):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert resp.json()["uid"] == str(fixed)
