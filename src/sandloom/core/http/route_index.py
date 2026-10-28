"""Route dispatch index: skip routes whose literal path prefix cannot match.

Starlette's :class:`~starlette.routing.Router` tries every route in
registration order and calls its ``matches`` (a regex match) until one fully
matches. With ~80 routes that linear scan costs tens of microseconds of CPU
per request, almost all of it spent on routes whose literal leading segments
(``/auth/...``, ``/api/v1/orgs/...``) can never match the request.

:class:`RouteIndex` keys every route by its leading LITERAL path segments (up
to :data:`MAX_DEPTH`; it stops at the first segment holding a ``{param}``)
and arranges the keys in a small trie. A request walks the trie along its own
leading segments as deep as it can and scans only that node's candidates:
every route whose key is a prefix of the walked path, in the ORIGINAL
registration order. Routes the index cannot reason about — a parameterised
first segment (``/{tenant}/...``), ``Mount`` / ``Host`` or any route class
with its own ``matches`` — have the empty key and so are candidates for every
request.

A route left out of a request's candidates requires a literal segment the
request does not have, so its regex can neither fully nor partially match:
dispatch is observably identical to the linear scan — same first FULL match
(registration-order precedence), same first PARTIAL match (405), same
``redirect_slashes`` probe, same 404 fallback, same websocket handling.

The index is rebuilt lazily whenever the router's route list no longer holds
exactly the routes it was built from (replaced, resized, or edited in place),
so routes added or swapped after :func:`install_route_index` — e.g. by a
composition root or a test — are picked up (and a late route with a durable
``idempotency_guard`` gets its response-capture wrapper,
:func:`sandloom.core.idempotency.durable.install_durable_capture`, before it serves).
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Sequence

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.datastructures import URL
from starlette.responses import RedirectResponse
from starlette.routing import BaseRoute, Match, Route, Router, WebSocketRoute
from starlette.types import Receive, Scope, Send

from sandloom.core.idempotency.durable import install_durable_capture

logger = logging.getLogger("sandloom.core.http.route_index")

#: How many leading literal segments a route is keyed by. Four, so the
#: kernel-mounted console routes of each module (``/api/v1/platform/<module>``,
#: about 11 per module) get a bucket per module: at three they all shared
#: ``/api/v1/platform`` and that bucket grew with every module loaded.
MAX_DEPTH = 4

#: ``matches`` implementations known to be "regex over the route path" (plus
#: FastAPI's wrappers that only annotate the child scope). A route whose class
#: overrides ``matches`` any other way is never excluded from a scan.
_REGEX_MATCHERS = frozenset(
    {
        Route.matches,
        WebSocketRoute.matches,
        APIRoute.matches,
        APIWebSocketRoute.matches,
    }
)

_Key = tuple[str, ...]


def _route_key(route: BaseRoute) -> _Key:
    """The leading literal segments a route requires (``()``: any path)."""
    if getattr(type(route), "matches", None) not in _REGEX_MATCHERS:
        return ()  # Mount / Host / custom routes: candidates for every path
    path = getattr(route, "path", None)
    if not isinstance(path, str) or not path.startswith("/"):
        return ()
    key: list[str] = []
    for segment in path[1:].split("/")[:MAX_DEPTH]:
        if "{" in segment:
            break  # parameterised from here on
        key.append(segment)
    return tuple(key)


class _Node:
    __slots__ = ("children", "routes")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.routes: tuple[BaseRoute, ...] = ()


class RouteIndex:
    """Leading-literal-segment trie over a router's routes (order-preserving)."""

    def __init__(self, routes: Sequence[BaseRoute]) -> None:
        self._source = routes
        self._size = len(routes)
        self._all: tuple[BaseRoute, ...] = tuple(routes)
        keyed = [(position, _route_key(route)) for position, route in enumerate(routes)]
        self._root = _Node()
        prefixes: dict[_Key, _Node] = {(): self._root}
        for _position, key in keyed:
            node = self._root
            for depth in range(1, len(key) + 1):
                prefix = key[:depth]
                child = prefixes.get(prefix)
                if child is None:
                    child = prefixes[prefix] = _Node()
                    node.children[prefix[-1]] = child
                node = child
        # A node's candidates: every route whose key is a prefix of the node's
        # path (equal included), in registration order.
        for prefix, node in prefixes.items():
            node.routes = tuple(
                routes[position] for position, key in keyed if key == prefix[: len(key)]
            )

    def is_current(self, routes: Sequence[BaseRoute]) -> bool:
        # Identity of every element, not just the length: an in-place edit
        # that keeps the size (``routes[:] = saved``, a swapped route) must
        # rebuild too. ~1 µs per 100 routes, done in C.
        return (
            routes is self._source
            and len(routes) == self._size
            and all(map(operator.is_, routes, self._all))
        )

    def candidates(self, route_path: str) -> tuple[BaseRoute, ...]:
        """Routes that may match ``route_path``, in registration order."""
        if not route_path.startswith("/"):
            return self._all
        node = self._root
        for segment in route_path[1:].split("/", MAX_DEPTH)[:MAX_DEPTH]:
            child = node.children.get(segment)
            if child is None:
                break
            node = child
        return node.routes

    @property
    def bucket_sizes(self) -> dict[str, int]:
        """Candidate count per indexed prefix (``"/api/v1/orgs"`` -> n)."""
        sizes: dict[str, int] = {}
        stack: list[tuple[str, _Node]] = [("", self._root)]
        while stack:
            path, node = stack.pop()
            sizes[path or "/*"] = len(node.routes)
            stack.extend(
                (f"{path}/{seg}", child) for seg, child in node.children.items()
            )
        return sizes


class IndexedDispatch:
    """Drop-in for ``Router.app`` that scans only the indexed candidates.

    Mirrors :meth:`starlette.routing.Router.app` step for step (FULL match,
    first PARTIAL match, ``redirect_slashes`` probe, ``default``).
    """

    def __init__(self, router: Router) -> None:
        self.router = router
        self._index = RouteIndex(router.routes)

    @property
    def index(self) -> RouteIndex:
        routes = self.router.routes
        if not self._index.is_current(routes):
            self._index = RouteIndex(routes)
            # Routes added after startup hand their response to a durable
            # idempotency claim too (create_app wrapped only the first set).
            install_durable_capture(routes)
        return self._index

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: C901
        router = self.router
        assert scope["type"] in ("http", "websocket", "lifespan")  # noqa: S101 - type narrowing of an invariant
        if "router" not in scope:
            scope["router"] = router
        if scope["type"] == "lifespan":
            await router.lifespan(scope, receive, send)
            return

        index = self.index
        route_path = _route_path(scope)
        partial: BaseRoute | None = None
        partial_scope: Scope = {}
        for route in index.candidates(route_path):
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                await route.handle(scope, receive, send)
                return
            if match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            scope.update(partial_scope)
            await partial.handle(scope, receive, send)
            return

        if scope["type"] == "http" and router.redirect_slashes and route_path != "/":
            redirect_scope = dict(scope)
            if route_path.endswith("/"):
                redirect_scope["path"] = redirect_scope["path"].rstrip("/")
            else:
                redirect_scope["path"] = redirect_scope["path"] + "/"
            for route in index.candidates(_route_path(redirect_scope)):
                match, _child_scope = route.matches(redirect_scope)
                if match != Match.NONE:
                    response = RedirectResponse(url=str(URL(scope=redirect_scope)))
                    await response(scope, receive, send)
                    return

        await router.default(scope, receive, send)


def _route_path(scope: Scope) -> str:
    # Same as starlette._utils.get_route_path (kept local: private module).
    path: str = scope["path"]
    root_path = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def install_route_index(router: Router) -> bool:
    """Route ``router`` through :class:`IndexedDispatch`.

    Only when the router has no router-level middleware (its
    ``middleware_stack`` is still its own ``app``); returns whether it was
    installed. Idempotent.
    """
    stack = router.middleware_stack
    if isinstance(stack, IndexedDispatch):
        return True
    if getattr(stack, "__func__", None) is not Router.app or (
        getattr(stack, "__self__", None) is not router
    ):
        logger.info("route index not installed: router has its own middleware")
        return False
    router.middleware_stack = IndexedDispatch(router)
    return True


__all__ = ["IndexedDispatch", "RouteIndex", "install_route_index"]
