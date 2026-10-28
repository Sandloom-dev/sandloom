"""Architecture rule: every HTTP route resolves one model E policy (ADR-0009).

The ADR-0003 operation classes are gone (removed outright, unreleased): the
route policy is DERIVED and deny-by-default. The node ids keep their
operation-class names for the guarantees ledger (G-OPCLASS-01/02 -> G-ROUTE-01
/04, G-OPCLASS-03 reworded); each now asserts the stronger replacement:

* every API route of the batteries monolith (``sandloom.main``: kernel ops routes +
  identity + tenancy + billing) is authenticated xor ``public(reason=)``, and
  ``create_app`` REFUSES a route that is neither (SANDLOOM-B008) — the old
  "unclassified" route only failed CI;
* every module's routes equal its committed ``routes.snapshot.json`` — the
  module's own approval of each route's resolved policy (``sandloom routes
  snapshot --write``);
* the routes the KERNEL mounts (ops endpoints, the platform console's
  per-module views) carry the policy the kernel assigns, whatever modules are
  loaded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI

from sandloom.core import route_snapshots
from sandloom.core.descriptors.module_names import module_name
from sandloom.core.di import public, sensitive
from sandloom.core.route_policy import (
    RoutePolicyError,
    api_routes,
    resolve_route_policy,
    route_policy_problems,
    validate_route_policy,
)
from sandloom.shared.authz import current_context

#: The kernel's ops routes: probes are public, the rest take a service
#: credential (the internal / metrics token). Fixed: they do not depend on the
#: loaded modules. ``(auth, effect)``.
KERNEL_OPS: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/health/simple"): ("public", "read"),
    ("GET", "/health"): ("public", "read"),
    ("GET", "/ready"): ("public", "read"),
    ("GET", "/diagnostics/core"): ("service", "read"),
    ("GET", "/diagnostics/consumers"): ("service", "read"),
    ("GET", "/diagnostics/services"): ("service", "read"),
    ("GET", "/diagnostics/infra"): ("service", "read"),
    ("GET", "/contracts/schemas"): ("service", "read"),
    ("GET", "/contracts/persistence"): ("service", "read"),
    ("POST", "/contracts/diff"): ("service", "write"),
    ("GET", "/metrics"): ("service", "read"),
}


def kernel_platform_routes(source: str) -> dict[tuple[str, str], tuple[str, str]]:
    """The platform console views the kernel mounts for one module: operator
    (admin-risk) reads, and operator commands (admin-risk writes)."""
    reads = (
        "audit",
        "services",
        "outbox/dead",
        "streams/dlq",
        "consumers",
        "projections",
        "contracts",
        "modules",
        "settings",
    )
    routes = {
        ("GET", f"/api/v1/platform/{source}/{view}"): ("authenticated", "read")
        for view in reads
    }
    for command in ("outbox/requeue", "streams/replay"):
        routes[("POST", f"/api/v1/platform/{source}/{command}")] = (
            "authenticated",
            "write",
        )
    return routes


# --------------------------------------------------------------------------- #
# The platform
# --------------------------------------------------------------------------- #
def test_every_platform_route_has_one_consistent_class() -> None:
    from sandloom.main import app

    problems = route_policy_problems(app.routes)
    assert not problems, "model E route policy:\n  " + "\n  ".join(problems)
    for route in api_routes(app.routes):
        policy = resolve_route_policy(route)
        assert policy.auth in ("user", "service", "public"), route.path
        if policy.auth == "public":
            assert policy.public is not None and policy.public.reason, route.path


def test_module_routes_match_their_snapshots() -> None:
    from sandloom.main import app

    problems = route_snapshots.snapshot_problems(app)
    assert not problems, "\n".join(problems)


def test_kernel_routes_carry_the_kernel_classes() -> None:
    from sandloom.main import app

    expected = dict(KERNEL_OPS)
    for descriptor in app.state.module_summary.loaded:
        expected.update(kernel_platform_routes(module_name(descriptor.name)))
    kernel = route_snapshots.kernel_routes(app)
    actual = {
        (e["method"], e["path"]): (e["auth"], e["policy"]["effect"]) for e in kernel
    }
    assert actual == expected
    for entry in kernel:
        policy = entry["policy"]
        if entry["path"].startswith("/api/v1/platform/"):
            # the operator gate carries the admin risk (ADR-0009 item 6)
            assert "admin" in policy["risk"], entry
            if entry["method"] == "POST":
                assert policy["audit"] == "automatic", entry
                assert policy["idempotency"] == "explicit cache, fail-closed", entry
        elif entry["auth"] == "public":
            assert policy["public_reason"], entry


def test_a_stale_snapshot_names_the_route_and_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = APIRouter()
    router.add_api_route("/notes", _ok, dependencies=[Depends(current_context)])
    router.add_api_route(
        "/notes",
        _ok,
        methods=["POST"],
        dependencies=[Depends(current_context), sensitive("admin")],
    )
    app = _app(router)
    route_snapshots.stamp_routes(app.routes, "app.notes")
    monkeypatch.setattr(route_snapshots, "snapshot_path", lambda m: tmp_path / "s.json")
    (problem,) = route_snapshots.snapshot_problems(app, ["app.notes"])
    assert "is missing" in problem and "sandloom routes snapshot --write" in problem
    doc = route_snapshots.module_route_snapshots(app, ["app.notes"])["app.notes"]
    doc["routes"] = [r for r in doc["routes"] if r["method"] == "GET"]
    (tmp_path / "s.json").write_text(route_snapshots.render_snapshot(doc))
    (problem,) = route_snapshots.snapshot_problems(app, ["app.notes"])
    assert "new: POST /notes [authenticated, audit=automatic, effect=write" in problem
    # a weakened policy (the risk tag dropped) is a diff too
    doc = route_snapshots.module_route_snapshots(app, ["app.notes"])["app.notes"]
    for entry in doc["routes"]:
        entry["policy"].pop("risk", None)
    (tmp_path / "s.json").write_text(route_snapshots.render_snapshot(doc))
    (problem,) = route_snapshots.snapshot_problems(app, ["app.notes"])
    assert "changed: POST /notes" in problem and "risk=" in problem


# --------------------------------------------------------------------------- #
# The check itself (it must FAIL on violations)
# --------------------------------------------------------------------------- #
def _app(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


async def _ok() -> dict[str, str]:
    return {}


def test_unclassified_route_fails_the_check() -> None:
    """A route with no authenticator and no public() is refused — at startup
    now, not only in CI (stronger than the old unclassified-route gate)."""
    router = APIRouter(prefix="/x")
    router.add_api_route("/classified", _ok, dependencies=[public(reason="probe")])
    router.add_api_route("/forgotten", _ok, methods=["POST"])
    problems = route_policy_problems(_app(router).routes)
    assert len(problems) == 1
    assert problems[0].startswith("POST /x/forgotten: no authenticator and not public")
    with pytest.raises(RoutePolicyError, match="SANDLOOM-B008"):
        validate_route_policy(_app(router).routes)


def test_router_level_class_covers_its_routes() -> None:
    router = APIRouter(dependencies=[public(reason="open data")])
    router.add_api_route("/a", _ok, methods=["POST"])
    router.add_api_route("/b", _ok, methods=["DELETE"])
    app = _app(router)
    assert route_policy_problems(app.routes) == []
    policies = {
        (m, str(r.path)): resolve_route_policy(r, m)
        for r in api_routes(app.routes)
        for m in r.methods
    }
    assert set(policies) == {("POST", "/a"), ("DELETE", "/b")}
    assert all(
        p.auth == "public" and p.public is not None and p.public.reason == "open data"
        for p in policies.values()
    )


def test_two_different_classes_fail() -> None:
    """Contradictory declarations are refused: public() on an authenticated
    route (and two different public reasons)."""
    router = APIRouter(dependencies=[public(reason="x")])
    router.add_api_route("/r", _ok, dependencies=[Depends(current_context)])
    router.add_api_route(
        "/two", _ok, methods=["POST"], dependencies=[public(reason="y")]
    )
    problems = route_policy_problems(_app(router).routes)
    assert len(problems) == 2
    assert any("declares public() and authenticates" in p for p in problems)
    assert any("different reasons" in p for p in problems)
    with pytest.raises(RoutePolicyError):
        validate_route_policy(_app(router).routes)


def test_budget_outside_class_r_fails() -> None:
    """No route of any class can declare a staleness budget (ADR-0009)."""
    import sandloom.core.di as di

    assert not hasattr(di, "revocation_freshness")


def test_strict_classes_must_be_revocation_strict() -> None:
    """Every route is revocation-strict: the authenticator reads the subject
    record live on every request; there is no marker to forget."""
    import sandloom.core.di as di

    assert not hasattr(di, "strict_revocation")


def test_strict_path_prefix_satisfies_strictness() -> None:
    """The prefix setting is gone (REMOVED, inert ``[]``): nothing to configure."""
    from sandloom.core.config import tiers
    from sandloom.core.config.settings import settings

    assert not hasattr(settings, "auth_revocation_strict_path_prefixes")
    assert "AUTH_REVOCATION_STRICT_PATH_PREFIXES" in {r.name for r in tiers.REMOVED}


@pytest.mark.parametrize("name", ["p", "X", "", "RW"])
def test_unknown_class_is_refused(name: str) -> None:
    """The operation classes are removed outright; an unknown risk is refused."""
    import sandloom.core.di as di

    assert not hasattr(di, "operation_class")
    with pytest.raises(RoutePolicyError, match="risk must be one of"):
        sensitive(name)  # type: ignore[arg-type]
