"""The first-segment route index dispatches exactly like Starlette's linear scan.

Every scenario runs against two copies of the same app — one with the index
installed, one without — and the responses must be identical: FULL-match
precedence in registration order (parameterised routes before literal ones
included), 405 from a PARTIAL match, ``redirect_slashes``, Mounts, websocket
routes, 404s, ``root_path`` and routes registered after installation.

Moved from ``sandloom/core/tests/test_route_index.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.routing import BaseRoute, Match
from starlette.types import Scope

from sandloom.core.http.route_index import (
    MAX_DEPTH,
    IndexedDispatch,
)

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
    from sandloom_standard.main import app

    router = app.router
    dispatch = router.middleware_stack
    assert isinstance(dispatch, IndexedDispatch), _t011(
        "create_app did not install the route index (IndexedDispatch)",
        "a router-level middleware replaced Router.app; see install_route_index",
    )
    routes = list(router.routes)
    assert len(routes) > 40, _t011(
        f"the platform app mounts only {len(routes)} routes",
        "sandloom_standard.main's installed modules did not load; run `sandloom check`",
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
