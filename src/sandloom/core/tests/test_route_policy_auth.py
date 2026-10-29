"""Deny-by-default (ADR-0009 item 1, spec §5.1, SANDLOOM-B008).

Every API route resolves an authenticator (``@authenticates`` for a user,
``@authenticates(kind="service")`` for a system credential) or declares
``public(reason=...)``; ``create_app`` refuses a route that is neither, or
both, and the message names the fix. These tests drive the check directly
and through ``create_app``, plus the composed platform (every route resolves).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, WebSocket

from sandloom.core import error_codes
from sandloom.core.guard_order import authenticates, authenticator_kind
from sandloom.core.route_markers import prune_marker_dependencies
from sandloom.core.route_policy import (
    RoutePolicyError,
    no_idempotency,
    public,
    replay_safe,
    resolve_route_policy,
    route_policy_problems,
    sensitive,
    validate_route_policy,
)


@authenticates(kind="service")
async def _service_token(x_token: str | None = Header(default=None)) -> None:
    return None


def _app(*routes: tuple[str, str, list[Any]]) -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    async def _ok() -> dict[str, str]:
        return {}

    for method, path, deps in routes:
        router.add_api_route(path, _ok, methods=[method], dependencies=deps)
    app.include_router(router)
    return app


def _user() -> Any:
    from sandloom.shared.authz import current_context

    return Depends(current_context)


def test_an_unauthenticated_untagged_route_is_refused_with_the_fix() -> None:
    app = _app(("POST", "/notes", []))
    (problem,) = route_policy_problems(app.routes)
    assert problem.startswith("POST /notes: no authenticator and not public.")
    assert 'public(reason="...")' in problem and "CurrentUser" in problem
    with pytest.raises(RoutePolicyError) as exc:
        validate_route_policy(app.routes)
    assert error_codes.code_for_exception(exc.value) == "SANDLOOM-B008"
    assert "sandloom explain SANDLOOM-B008" in str(exc.value)


def test_public_and_an_authenticator_together_are_refused() -> None:
    app = _app(("GET", "/x", [public(reason="probe"), _user()]))
    (problem,) = route_policy_problems(app.routes)
    assert "declares public() and authenticates" in problem
    assert "current_context" in problem and problem.endswith("Fix: remove one.")


def test_public_with_a_reason_passes_and_is_recorded() -> None:
    app = _app(
        ("POST", "/login", [public(reason="password login", issues_tokens=True)])
    )
    assert route_policy_problems(app.routes) == []
    policy = resolve_route_policy(app.routes[-1])
    assert policy.auth == "public"
    assert policy.public is not None and policy.public.issues_tokens
    assert policy.public.reason == "password login"


def test_a_service_gated_route_passes() -> None:
    app = _app(("GET", "/ops", [Depends(_service_token)]))
    assert route_policy_problems(app.routes) == []
    assert resolve_route_policy(app.routes[-1]).auth == "service"
    assert authenticator_kind(_service_token) == "service"


def test_a_user_route_passes_and_is_user() -> None:
    app = _app(("PATCH", "/me", [_user()]))
    assert route_policy_problems(app.routes) == []
    policy = resolve_route_policy(app.routes[-1])
    assert (policy.auth, policy.effect) == ("user", "write")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: public(reason=""),
        lambda: public(reason="   "),
        lambda: no_idempotency(reason=""),
        lambda: replay_safe(reason=" "),
    ],
)
def test_an_empty_reason_is_refused_at_declaration(factory: Any) -> None:
    with pytest.raises(RoutePolicyError, match="needs a reason"):
        factory()


def test_an_unknown_risk_or_kind_is_refused() -> None:
    with pytest.raises(RoutePolicyError):
        sensitive("data")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        authenticates(kind="robot")(lambda: None)  # type: ignore[arg-type]


def test_router_level_public_covers_its_routes() -> None:
    app = FastAPI()
    router = APIRouter(dependencies=[public(reason="a public service")])

    @router.post("/a")
    async def _a() -> dict[str, str]:
        return {}

    app.include_router(router)
    assert route_policy_problems(app.routes) == []


def test_the_markers_are_pruned_and_still_read() -> None:
    app = _app(("GET", "/x", [public(reason="probe")]))
    assert prune_marker_dependencies(app.routes) == 1
    assert route_policy_problems(app.routes) == []
    assert resolve_route_policy(app.routes[-1]).public is not None


def test_websocket_and_plain_routes_are_out_of_scope() -> None:
    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket) -> None:  # pragma: no cover - not served
        await websocket.close()

    app.add_route("/raw", lambda request: None)
    assert route_policy_problems(app.routes) == []


def test_create_app_refuses_an_unauthenticated_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core import app_factory

    real = app_factory._validate_and_index_routes

    def _with_bad_route(application: FastAPI) -> None:
        @application.post("/bad")
        async def _bad() -> dict[str, str]:
            return {}

        real(application)

    monkeypatch.setattr(app_factory, "_validate_and_index_routes", _with_bad_route)
    for name in ("set_active_infra", "set_active_event_bus", "set_active_core"):
        monkeypatch.setattr(app_factory, name, lambda *_a, **_k: None)
    with pytest.raises(RoutePolicyError, match="POST /bad: no authenticator"):
        app_factory.create_app()


def test_every_platform_route_is_authenticated_or_public() -> None:
    """The composed batteries monolith resolves (create_app ran the check);
    the kernel's probes are the only public kernel routes, every other ops
    route is service-gated, the platform console is a user route."""
    from sandloom.main import app

    kernel = {
        (m, r.path): resolve_route_policy(r, m)
        for r in app.routes
        if getattr(r, "dependant", None) is not None and getattr(r, "methods", None)
        for m in r.methods
        if not getattr(r, "sandloom_module", None)
    }
    publics = {k for k, p in kernel.items() if p.auth == "public"}
    assert {path for _m, path in publics} == {"/health/simple", "/health", "/ready"}
    for (method, path), policy in kernel.items():
        if path.startswith(("/diagnostics/", "/contracts/")) or path == "/metrics":
            assert policy.auth == "service", (method, path)
        if path.startswith("/api/v1/platform/"):
            assert policy.auth == "user", (method, path)
    assert route_policy_problems(app.routes) == []


def test_bundled_public_routes_are_exactly_the_reviewed_set() -> None:
    """Independent oracle (spec §10.1): the hand-kept list of the platform's
    public module routes; a new public route must be added here on purpose."""
    from sandloom.main import app

    expected = {
        ("GET", "/auth/health"),
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/register"),
        ("POST", "/auth/request-password-reset"),
        ("POST", "/auth/resend-verification"),
        ("POST", "/auth/reset-password"),
        ("POST", "/auth/sso/mfa"),
        ("GET", "/auth/sso/providers"),
        ("GET", "/auth/sso/{provider}/callback"),
        ("GET", "/auth/sso/{provider}/redirect"),
        ("POST", "/auth/verify-email"),
        ("GET", "/api/v1/billing/plans"),
        ("POST", "/api/v1/billing/webhooks/stripe"),
    }
    issuers = {
        ("POST", "/auth/login"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/sso/mfa"),
        ("GET", "/auth/sso/{provider}/callback"),
    }
    found: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        if not getattr(route, "sandloom_module", None):
            continue
        for method in getattr(route, "methods", ()) or ():
            policy = resolve_route_policy(route, method)
            if policy.auth == "public" and method != "HEAD":
                found[(method, route.path)] = policy
    assert set(found) == expected
    assert {k for k, p in found.items() if p.public and p.public.issues_tokens} == (
        issuers
    )
    # a public route that reads a subject would be a contradiction
    assert all(p.authenticators == () for p in found.values())


def test_extraction_probe_declares_its_test_only_public_routes(
    monkeypatch: Any,
) -> None:
    """Exercise the actual probe fixture with the real startup policy gate."""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location(
        "_extraction_probe_policy_test",
        root / "tests/extraction/extract_probe/__init__.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    routes = module.router.routes
    assert {route.path for route in routes} == {
        "/_x/spend",
        "/_x/balance/{tenant_type}/{tenant_id}",
    }
    validate_route_policy(routes)
    assert all(resolve_route_policy(route).auth == "public" for route in routes)
