"""Startup check: an ``idempotency_guard`` resolves AFTER authentication.

The guard reserves (and replays) a response under a key scoped to the caller.
Resolved before the route's authentication dependency, it reserves for a
caller the route has not authenticated yet: an invalid / revoked / blocked
token, or one the authentication dependency would refuse for its type or
claims, reaches the idempotency store first, and a replay short-circuits every
dependency FastAPI would have solved after the guard. The guard re-checks the
bearer's revocation / block state itself (A-03), but authentication is the
route's own dependency and must run first.

FastAPI solves a route's dependencies depth-first in declaration order —
router-level ``dependencies``, then the route's ``dependencies=[...]``, then
the endpoint's parameters in signature order — each dependency's own
sub-dependencies before it, a cached (already solved) one only once.
:func:`guard_order_problems` replays exactly that order over
``route.dependant`` and reports a route whose first idempotency guard is
solved before its first authenticating dependency.

What counts:

* an idempotency guard — a dependency built by
  :func:`sandloom.core.idempotency.idempotency_guard` (registered here);
* an authenticating dependency — a callable registered with
  :func:`authenticates` (``sandloom.core.di.authenticates`` for app code). The
  framework registers its own: ``sandloom.shared.authz.current_context`` (and so
  everything built on it: ``get_current_user_id``, ``require(...)``,
  ``authorize_resource``) and the platform console's operator guard. A route
  with a guard and no authenticating dependency (an anonymous endpoint) is not
  reported: the guard is anonymous-safe (status-only replay).

``create_app`` refuses to start on a reported route
(:class:`GuardOrderError`). Fix: declare the guard after the authentication —
as an endpoint parameter after ``user_id = Depends(get_current_user_id)``, or
put the authentication in the router / route ``dependencies`` before it.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Literal, TypeVar, overload

_F = TypeVar("_F", bound=Callable[..., Any])

#: What an authenticator proves: a USER subject (a bearer access token, read
#: live against the subject-state record) or a SERVICE (a system credential:
#: the internal / metrics token). Model E (ADR-0009) applies the user
#: defaults (commit-time re-check, no-store, automatic idempotency) to routes
#: with a user authenticator only.
AuthenticatorKind = Literal["user", "service"]
AUTHENTICATOR_KINDS: tuple[str, ...] = ("user", "service")

_AUTHENTICATORS: weakref.WeakKeyDictionary[Callable[..., Any], str] = (
    weakref.WeakKeyDictionary()
)
_GUARDS: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()


class GuardOrderError(RuntimeError):
    """A route solves an idempotency guard before its authentication."""


@overload
def authenticates(fn: _F, *, kind: AuthenticatorKind = "user") -> _F: ...
@overload
def authenticates(
    fn: None = None, *, kind: AuthenticatorKind = "user"
) -> Callable[[_F], _F]: ...
def authenticates(
    fn: _F | None = None, *, kind: AuthenticatorKind = "user"
) -> _F | Callable[[_F], _F]:
    """Mark ``fn`` (a FastAPI dependency) as authenticating the caller.

    ``@authenticates`` (a user subject) or ``@authenticates(kind="service")``
    (a system credential). Deny-by-default (ADR-0009, SANDLOOM-B008): every API
    route resolves an authenticator or declares ``public(reason=...)``.
    """
    if kind not in AUTHENTICATOR_KINDS:
        raise ValueError(
            f"authenticator kind must be 'user' or 'service', not {kind!r}"
        )

    def _mark(target: _F) -> _F:
        _AUTHENTICATORS[target] = kind
        return target

    return _mark if fn is None else _mark(fn)


#: Custom authorization dependencies (``authorizes``), recorded per route in
#: its snapshot (``authz``) so a reviewer sees who decides access.
_AUTHORIZERS: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()


def authorizes(fn: _F) -> _F:
    """Mark ``fn`` (a FastAPI dependency) as a custom authorization check.

    Recorded, not enforced: the route snapshot lists the marked dependencies a
    route resolves (``authz``, ADR-0009 "The route snapshot"), so dropping a
    hand-rolled check shows up as a snapshot diff (SANDLOOM-T001). It neither
    authenticates (``@authenticates`` does) nor carries a risk
    (``sensitive(...)`` does).
    """
    _AUTHORIZERS.add(fn)
    return fn


def authorizer_names(dependant: Any) -> tuple[str, ...]:
    """The ``module:qualname`` of every ``authorizes`` dependency a route
    resolves, in resolution order (empty: none)."""
    names: list[str] = []
    for call in resolution_order(dependant):
        if _member(call, _AUTHORIZERS):
            module = getattr(call, "__module__", "?")
            name = f"{module}:{getattr(call, '__qualname__', repr(call))}"
            if name not in names:
                names.append(name)
    return tuple(names)


def mark_idempotency_guard(fn: _F) -> _F:
    """Record ``fn`` as an idempotency guard (``idempotency_guard`` only)."""
    _GUARDS.add(fn)
    return fn


def _member(call: Any, registry: weakref.WeakSet[Callable[..., Any]]) -> bool:
    if call is None:
        return False
    try:
        return call in registry
    except TypeError:  # unhashable / not weak-referenceable
        return False


def is_authenticator(call: Any) -> bool:
    return authenticator_kind(call) is not None


def authenticator_kind(call: Any) -> str | None:
    """``"user"`` / ``"service"`` for an authenticator, else ``None``."""
    if call is None:
        return None
    try:
        return _AUTHENTICATORS.get(call)
    except TypeError:  # unhashable / not weak-referenceable
        return None


def is_idempotency_guard(call: Any) -> bool:
    return _member(call, _GUARDS)


def resolution_order(dependant: Any) -> list[Any]:
    """The calls of ``dependant``'s dependencies in the order FastAPI runs them.

    Depth-first, sub-dependencies first; a call already solved (FastAPI's
    per-request dependency cache) is not repeated.
    """
    order: list[Any] = []
    seen: set[int] = set()

    def visit(node: Any) -> None:
        for sub in getattr(node, "dependencies", None) or ():
            visit(sub)
            call = getattr(sub, "call", None)
            if call is None or id(call) in seen:
                continue
            seen.add(id(call))
            order.append(call)

    visit(dependant)
    return order


def iter_api_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """The HTTP API routes of ``routes`` (those with a dependant and methods)."""
    for route in routes:
        if getattr(route, "dependant", None) is not None and hasattr(route, "methods"):
            yield route


def guard_order_problems(routes: Iterable[Any]) -> list[str]:
    """Every route whose idempotency guard is solved before its authentication."""
    problems: list[str] = []
    for route in iter_api_routes(routes):
        order = resolution_order(route.dependant)
        guard_at = next(
            (i for i, call in enumerate(order) if is_idempotency_guard(call)), None
        )
        auth_at = next(
            (i for i, call in enumerate(order) if is_authenticator(call)), None
        )
        if guard_at is None or auth_at is None or auth_at < guard_at:
            continue
        methods = ",".join(sorted(route.methods or ())) or "?"
        problems.append(
            f"{methods} {route.path}: idempotency_guard resolves before the "
            "route's authentication — declare it after the auth dependency "
            "(an endpoint parameter after Depends(get_current_user_id), or "
            "the auth in the router / route dependencies ahead of it)"
        )
    return problems


def validate_guard_order(routes: Iterable[Any]) -> None:
    """Startup check: raise :class:`GuardOrderError` listing every problem."""
    problems = guard_order_problems(routes)
    if problems:
        raise GuardOrderError("; ".join(problems))


class GateOrderError(RuntimeError):
    """A route solves an organisation-scoped authz gate before TenantScope()."""

    sandloom_code = "SANDLOOM-B007"


def _path_params(path: str) -> set[str]:
    return {
        seg[1:-1].split(":")[0]
        for seg in path.split("/")
        if seg.startswith("{") and seg.endswith("}")
    }


def gate_order_problems(routes: Iterable[Any]) -> list[str]:
    """Routes whose org-scoped gate is solved before their ``TenantScope()``.

    A gate (``require(...)`` of ``sandloom.shared.authz``) records the path
    parameters it may read an organisation from (``__sandloom_org_params__``); the
    ``TenantScope()`` dependency is tagged ``__sandloom_binds_tenant__``. When the
    route's path carries none of the gate's parameters, the gate falls back to
    the bound tenant — which is unbound if TenantScope() is solved after it
    (FastAPI solves decorator dependencies before endpoint parameters), so
    every call fails with "organization context required" (agent A's trap).
    """
    problems: list[str] = []
    for route in iter_api_routes(routes):
        order = resolution_order(route.dependant)
        binder = next(
            (
                i
                for i, c in enumerate(order)
                if getattr(c, "__sandloom_binds_tenant__", False)
            ),
            None,
        )
        if binder is None:
            continue
        params = _path_params(str(route.path))
        for call in order[:binder]:
            needed = getattr(call, "__sandloom_org_params__", None)
            if needed and not set(needed) & params:
                methods = ",".join(sorted(route.methods or ())) or "?"
                problems.append(
                    f"{methods} {route.path}: a require(...) gate that needs an "
                    "organisation resolves before TenantScope() — declare "
                    "TenantScope() first (put the gate in the endpoint signature "
                    "after the TenantScope() parameter, or TenantScope() in the "
                    "router dependencies)"
                )
                break
    return problems


def validate_gate_order(routes: Iterable[Any]) -> None:
    """Startup check: raise :class:`GateOrderError` listing every problem."""
    problems = gate_order_problems(routes)
    if problems:
        raise GateOrderError("; ".join(problems) + " (sandloom explain SANDLOOM-B007)")


__all__ = [
    "AUTHENTICATOR_KINDS",
    "AuthenticatorKind",
    "GateOrderError",
    "GuardOrderError",
    "authenticates",
    "authenticator_kind",
    "authorizer_names",
    "authorizes",
    "gate_order_problems",
    "guard_order_problems",
    "is_authenticator",
    "is_idempotency_guard",
    "iter_api_routes",
    "mark_idempotency_guard",
    "resolution_order",
    "validate_gate_order",
    "validate_guard_order",
]
