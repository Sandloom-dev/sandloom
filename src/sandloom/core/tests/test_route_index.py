"""The first-segment route index dispatches exactly like Starlette's linear scan.

Every scenario runs against two copies of the same app — one with the index
installed, one without — and the responses must be identical: FULL-match
precedence in registration order (parameterised routes before literal ones
included), 405 from a PARTIAL match, ``redirect_slashes``, Mounts, websocket
routes, 404s, ``root_path`` and routes registered after installation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute, Match, Mount, Route, Router
from starlette.types import Scope

from sandloom.core.http.route_index import (
    MAX_DEPTH,
    IndexedDispatch,
    RouteIndex,
    install_route_index,
)


def _build() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"route": "root"}

    # Parameterised FIRST segment registered before a literal route that it
    # also matches: the param route must keep winning (registration order).
    @app.get("/{section}/shadowed")
    async def param_first(section: str) -> dict[str, str]:
        return {"route": "param-first", "section": section}

    @app.get("/users/shadowed")
    async def users_shadowed() -> dict[str, str]:
        return {"route": "users-shadowed"}

    # The same precedence one trie level deeper (/api/v1/<param> first).
    @app.get("/api/v1/{resource}/x")
    async def deep_param(resource: str) -> dict[str, str]:
        return {"route": "deep-param", "resource": resource}

    @app.get("/api/v1/orgs/x")
    async def deep_literal() -> dict[str, str]:
        return {"route": "deep-literal"}

    @app.get("/api/v1/orgs/{org_id}/members")
    async def deep_members(org_id: int) -> dict[str, Any]:
        return {"route": "deep-members", "org": org_id}

    @app.get("/users/me")
    async def users_me() -> dict[str, str]:
        return {"route": "users-me"}

    @app.get("/users/{user_id}")
    async def user(user_id: int) -> dict[str, Any]:
        return {"route": "user", "id": user_id}

    # Same path, two methods registered separately: GET-only first, so a POST
    # PARTIAL-matches it before FULL-matching the second route.
    @app.get("/items")
    async def list_items() -> dict[str, str]:
        return {"route": "items-get"}

    @app.post("/items")
    async def create_item() -> dict[str, str]:
        return {"route": "items-post"}

    @app.put("/only-put")
    async def only_put() -> dict[str, str]:
        return {"route": "only-put"}

    @app.get("/trailing/")
    async def trailing() -> dict[str, str]:
        return {"route": "trailing"}

    @app.websocket("/ws/echo")
    async def ws_echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("echo:" + await websocket.receive_text())
        await websocket.close()

    sub = Router(routes=[Route("/file", endpoint=_plain("mounted-file"))])
    app.mount("/static", sub)
    return app


def _plain(label: str) -> Callable[..., Any]:
    from starlette.responses import JSONResponse

    async def endpoint(request: Any) -> Any:
        return JSONResponse({"route": label, "path": request.url.path})

    return endpoint


@pytest.fixture
def clients() -> tuple[TestClient, TestClient]:
    linear, indexed = _build(), _build()
    assert install_route_index(indexed.router) is True
    assert isinstance(indexed.router.middleware_stack, IndexedDispatch)
    return (
        TestClient(linear, follow_redirects=False),
        TestClient(indexed, follow_redirects=False),
    )


def _same(clients: tuple[TestClient, TestClient], method: str, path: str) -> Any:
    linear, indexed = clients
    a = linear.request(method, path)
    b = indexed.request(method, path)
    assert (a.status_code, a.content) == (b.status_code, b.content), path
    for header in ("allow", "location"):
        assert a.headers.get(header) == b.headers.get(header), (path, header)
    return b


@pytest.mark.parametrize(
    ("method", "path", "status", "route"),
    [
        ("GET", "/", 200, "root"),
        ("GET", "/users/me", 200, "users-me"),
        ("GET", "/users/42", 200, "user"),
        ("GET", "/users/shadowed", 200, "param-first"),  # order precedence
        ("GET", "/anything/shadowed", 200, "param-first"),
        ("GET", "/items", 200, "items-get"),
        ("POST", "/items", 200, "items-post"),  # PARTIAL then FULL
        ("GET", "/static/file", 200, "mounted-file"),
        ("GET", "/api/v1/orgs/x", 200, "deep-param"),  # order, one level down
        ("GET", "/api/v1/orgs/5/members", 200, "deep-members"),
    ],
)
def test_full_matches_are_identical(
    clients: tuple[TestClient, TestClient],
    method: str,
    path: str,
    status: int,
    route: str,
) -> None:
    response = _same(clients, method, path)
    assert response.status_code == status
    assert response.json()["route"] == route


def test_405_comes_from_the_first_partial_match(
    clients: tuple[TestClient, TestClient],
) -> None:
    response = _same(clients, "DELETE", "/items")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"  # the FIRST partial (GET route)
    assert _same(clients, "GET", "/only-put").status_code == 405


def test_404_and_unknown_segments(clients: tuple[TestClient, TestClient]) -> None:
    assert _same(clients, "GET", "/nope").status_code == 404
    assert _same(clients, "GET", "/nope/deeper/still").status_code == 404
    assert _same(clients, "GET", "/users/not-an-int/x").status_code == 404
    assert _same(clients, "GET", "/static/missing").status_code == 404


def test_redirect_slashes(clients: tuple[TestClient, TestClient]) -> None:
    added = _same(clients, "GET", "/trailing")
    assert added.status_code == 307
    assert added.headers["location"].endswith("/trailing/")
    stripped = _same(clients, "GET", "/users/me/")
    assert stripped.status_code == 307
    assert stripped.headers["location"].endswith("/users/me")
    # A Mount reached without its trailing slash redirects too.
    assert _same(clients, "GET", "/static").status_code in (307, 404)


def test_redirect_slashes_disabled_is_honoured() -> None:
    linear, indexed = _build(), _build()
    for app in (linear, indexed):
        app.router.redirect_slashes = False
    install_route_index(indexed.router)
    pair = (
        TestClient(linear, follow_redirects=False),
        TestClient(indexed, follow_redirects=False),
    )
    assert _same(pair, "GET", "/trailing").status_code == 404


def test_websocket_routes(clients: tuple[TestClient, TestClient]) -> None:
    for client in clients:
        with client.websocket_connect("/ws/echo") as ws:
            ws.send_text("hi")
            assert ws.receive_text() == "echo:hi"
    from starlette.websockets import WebSocketDisconnect

    for client in clients:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/missing") as ws:
                ws.receive_text()


def test_routes_added_after_install_are_dispatched(
    clients: tuple[TestClient, TestClient],
) -> None:
    for client in clients:

        @client.app.get("/late/{n}")  # type: ignore[attr-defined, untyped-decorator]
        async def late(n: int) -> dict[str, Any]:
            return {"route": "late", "n": n}

    assert _same(clients, "GET", "/late/3").json() == {"route": "late", "n": 3}


@pytest.mark.parametrize("edit", ["swap", "restore_and_readd", "insert_remove"])
def test_in_place_route_edits_keeping_the_size_rebuild_the_index(edit: str) -> None:
    """Same-length in-place edits must not leave the index on stale routes."""
    linear, indexed = _build(), _build()
    install_route_index(indexed.router)
    pair = (
        TestClient(linear, follow_redirects=False),
        TestClient(indexed, follow_redirects=False),
    )
    _same(pair, "GET", "/users/me")  # build the index on the original routes
    for app in (linear, indexed):
        routes = app.router.routes
        if edit == "swap":
            position = next(
                i for i, r in enumerate(routes) if getattr(r, "path", "") == "/users/me"
            )
            routes[position] = Route("/users/me", endpoint=_plain("swapped"))
        elif edit == "restore_and_readd":
            saved = list(routes)

            @app.get("/extra/{n}")
            async def extra(n: int) -> dict[str, Any]:
                return {"route": "extra", "n": n}

            routes[:] = [*saved[:-1], routes[-1]]
        else:
            routes.insert(0, Route("/users/me", endpoint=_plain("inserted")))
            routes.pop()
    expected = {
        "swap": "swapped",
        "restore_and_readd": None,
        "insert_remove": "inserted",
    }
    response = _same(pair, "GET", "/users/me")
    if expected[edit] is not None:
        assert response.json()["route"] == expected[edit]
    if edit == "restore_and_readd":
        assert _same(pair, "GET", "/extra/2").json() == {"route": "extra", "n": 2}


def test_root_path_is_respected() -> None:
    linear, indexed = _build(), _build()
    install_route_index(indexed.router)
    pair = (
        TestClient(linear, root_path="/api", follow_redirects=False),
        TestClient(indexed, root_path="/api", follow_redirects=False),
    )
    assert _same(pair, "GET", "/api/users/me").json()["route"] == "users-me"
    assert _same(pair, "GET", "/users/me").json()["route"] == "users-me"


def test_custom_route_classes_and_mounts_are_in_every_bucket() -> None:
    class AnyPath(Route):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.FULL, {}

    custom = AnyPath("/custom", endpoint=_plain("custom"))
    mount = Mount("/static", routes=[])
    literal = Route("/users/me", endpoint=_plain("me"))
    param = Route("/{x}/y", endpoint=_plain("param"))
    index = RouteIndex([literal, custom, mount, param])
    assert index.candidates("/users/me") == (literal, custom, mount, param)
    assert index.candidates("/other/y") == (custom, mount, param)
    assert index.candidates("") == (literal, custom, mount, param)


def test_install_is_idempotent_and_skips_routers_with_middleware() -> None:
    app = _build()
    assert install_route_index(app.router) is True
    stack = app.router.middleware_stack
    assert install_route_index(app.router) is True
    assert app.router.middleware_stack is stack

    from starlette.middleware import Middleware
    from starlette.middleware.gzip import GZipMiddleware

    wrapped = Router(routes=[], middleware=[Middleware(GZipMiddleware)])
    assert install_route_index(wrapped) is False


# --------------------------------------------------------------------------- #
# The real platform app: for every registered route and method, the index
# resolves the same route as the linear scan.
# --------------------------------------------------------------------------- #

_PARAM = re.compile(r"\{([^}:]+)(?::([^}]+))?\}")


def _sample(path: str) -> str:
    def repl(m: re.Match[str]) -> str:
        convertor = m.group(2) or "str"
        return {
            "int": "7",
            "float": "1.5",
            "uuid": "7f1c7c1e-0000-4000-8000-000000000000",
            "path": "a/b",
        }.get(convertor, "sample")

    return _PARAM.sub(repl, path)


def _resolve(routes: Any, scope: Scope) -> tuple[BaseRoute | None, str]:
    partial = None
    for route in routes:
        match, _ = route.matches(dict(scope))
        if match == Match.FULL:
            return route, "full"
        if match == Match.PARTIAL and partial is None:
            partial = route
    return (partial, "partial") if partial is not None else (None, "none")


def test_platform_app_resolution_is_identical_to_linear_scan() -> None:
    from sandloom.main import app

    router = app.router
    dispatch = router.middleware_stack
    assert isinstance(dispatch, IndexedDispatch), _t011(
        "create_app did not install the route index (IndexedDispatch)",
        "a router-level middleware replaced Router.app; see install_route_index",
    )
    routes = list(router.routes)
    assert len(routes) > 40, _t011(
        f"the platform app mounts only {len(routes)} routes",
        "sandloom.main's installed modules did not load; run `sandloom check`",
    )
    paths = {_sample(getattr(r, "path", "/")) for r in routes}
    paths |= {p + "/" for p in paths} | {"/", "/nope", "/health", ""}
    checked = 0
    for path in sorted(paths):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            scope: Scope = {
                "type": "http",
                "method": method,
                "path": path,
                "root_path": "",
                "headers": [],
                "query_string": b"",
            }
            assert _resolve(routes, scope) == _resolve(
                dispatch.index.candidates(path), scope
            ), _t011(
                f"{method} {path} resolves to a different route through the "
                "index than through the linear scan",
                "a kernel bug in sandloom.core.http.route_index: report it with the "
                "route's path and class; a module cannot cause it",
            )
            checked += 1
    assert checked > 200, _t011(f"only {checked} paths sampled", "see above")
    # The index actually narrows the scan for the platform's prefixes.
    sizes = dispatch.index.bucket_sizes
    bucket, largest = max(sizes.items(), key=lambda item: item[1])
    assert largest < len(routes) / 2, _t011(
        f"the route-index bucket {bucket!r} holds {largest} of {len(routes)} "
        "routes, so dispatch degrades to a linear scan for it (performance, "
        "not correctness)",
        "routes of several modules share a literal prefix longer than "
        f"route_index.MAX_DEPTH ({MAX_DEPTH}): a kernel change, report it. "
        "If they are one module's routes, give that module distinct literal "
        "prefixes (/api/v1/<module>/<area>/...)",
    )


def _t011(problem: str, fix: str) -> str:
    return (
        f"error[SANDLOOM-T011]: {problem}. Fix: {fix}. (sandloom explain SANDLOOM-T011)"
    )


def test_platform_console_routes_get_a_bucket_per_module() -> None:
    """Each module's console routes (/api/v1/platform/<module>/...) are keyed
    by their module, so the platform bucket does not grow with modules."""
    routes = [
        Route(f"/api/v1/platform/m{i}/items/{{x}}", endpoint=_plain(str(i)))
        for i in range(8)
    ]
    index = RouteIndex(routes)
    assert index.candidates("/api/v1/platform/m3/items/1") == (routes[3],)
