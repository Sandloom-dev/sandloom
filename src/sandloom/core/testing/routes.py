"""Route-level gates for an app's own test suite (model E, ADR-0009).

:func:`assert_route_snapshot` pins a module's routes and their resolved policy
(SANDLOOM-T001); :func:`route_policy` reads the policy the framework applies to one
route (authentication, public reason, revocation) without reaching the
kernel's route-policy internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sandloom.core.route_policy import resolve_route_policy
from sandloom.core.route_snapshots import snapshot_problems


def assert_route_snapshot(app: Any, *modules: str) -> None:
    """Fail unless each module's routes equal its committed ``routes.snapshot.json``.

    The module's approval of its routes' resolved policy (SANDLOOM-T001): the
    snapshot lists method, path, auth and policy of every route the module mounts
    (kernel-mounted routes excluded). Regenerate with ``sandloom routes snapshot
    --write`` and review the diff. With no ``modules`` every loaded module is
    checked. A module test pins its own::

        def test_routes_are_approved() -> None:
            from sandloom.main import app

            assert_route_snapshot(app, "sandloom.notes")
    """
    problems = snapshot_problems(app, modules or None)
    if problems:
        raise AssertionError(
            "route snapshots (SANDLOOM-T001):\n  " + "\n  ".join(problems)
        )


@dataclass(frozen=True)
class RoutePolicy:
    """What the framework applies to one route (ADR-0009).

    ``auth`` is ``"user"``, ``"service"`` or ``"public"`` (deny-by-default:
    a public route always carries ``public_reason``). ``effect`` is ``"read"``
    or ``"write"`` (by method) and ``risk`` the risks resolved from their
    sources (an admin gate, a money or security effect, ``sensitive()``),
    sorted. Every route is strict:
    the subject-state record is read live on every authenticated request
    (``always_strict`` is True, ``declares_budget`` False and
    ``staleness_budget`` 0.0 for every route).
    """

    always_strict: bool
    declares_budget: bool
    staleness_budget: float
    auth: str = "user"
    public_reason: str | None = None
    effect: str = "read"
    risk: tuple[str, ...] = ()


def route_policy(route: Any) -> RoutePolicy:
    """The policy the framework applies to ``route`` (resolved live). An app
    asserts deny-by-default with it::

        for route in app.routes:
            policy = route_policy(route)
            assert policy.auth != "public" or policy.public_reason
    """
    resolved = resolve_route_policy(route)
    return RoutePolicy(
        always_strict=True,
        declares_budget=False,
        staleness_budget=0.0,
        auth=resolved.auth,
        public_reason=resolved.public.reason if resolved.public is not None else None,
        effect=resolved.effect,
        risk=tuple(sorted(resolved.risk)),
    )


__all__ = [
    "RoutePolicy",
    "assert_route_snapshot",
    "route_policy",
]
