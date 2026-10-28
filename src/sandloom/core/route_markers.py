"""Route markers: declarations written as no-op dependencies, read at startup.

Declarative route markers (``public(...)``, ``sensitive(...)``, ADR-0009) are declared
the FastAPI way — ``Depends(...)`` on a route or a router — but they do
nothing per request: the kernel reads them off the route (``create_app``'s
checks, the CI gate, the route snapshots). Left in place,
FastAPI still solves each of them on every request: ~35-40k instructions
(~6-8 µs) per marker per request (``benchmarks/ladder/RESULTS.md``).

:func:`prune_marker_dependencies` (called by ``create_app`` once every module
router is included and the declarations are validated) records each route's
markers on the route (:func:`route_marker_calls` reads them back) and removes
them from the dependencies FastAPI executes. Public semantics are unchanged:

* the declaration itself is untouched — ``route.dependencies`` (what
  ``include_router`` copies) still lists the markers, so a route re-included
  elsewhere carries them again;
* every reader of a marker goes through :func:`route_marker_calls`, which
  merges the pruned markers with whatever still sits in the dependant, so a
  route added after startup (not pruned) is read identically;
* OpenAPI is unaffected (a marker has no parameters, no security scheme, no
  scopes — it contributes nothing to the flattened dependant).

FastAPI surface touched: exactly one PUBLIC dataclass field,
``APIRoute.dependant.dependencies`` (the list of sub-``Dependant``), mutated
in place. No request handler is rebuilt or re-implemented: the handler
``APIRoute`` built holds the same ``Dependant`` and resolves its
``dependencies`` per request. Were a FastAPI version to copy that list at
construction instead, pruning would only stop saving time — never change
behaviour, because a marker is a no-op either way. The pruning runs only on a
FastAPI version inside :data:`VERIFIED_FASTAPI_RANGE` (where the test suite
proves the pruned markers are no longer called); on any other version it
leaves every marker in place and logs one warning.

Only HTTP ``APIRoute``\\ s are pruned (websocket routes keep their markers),
and only the route's own parameterless ``dependencies=[...]`` entries (route
or router level) that take no parameters and have no sub-dependencies. A
marker bound to a parameter of another dependency (``_m: None =
Depends(marker)``) feeds that call and stays executed.

A marker is a declaration, not a dependency to override:
``dependency_overrides`` keyed by a marker callable has no effect on a
pruned route (every other override is unaffected).
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable, Iterable, Iterator
from typing import Any, TypeVar

import fastapi
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

logger = logging.getLogger("sandloom.core.route_markers")

#: FastAPI versions on which pruning is verified (``[low, high)``; the
#: project pins ``fastapi>=0.136.3,<0.137``). Widen together with the pin,
#: after the suite (``test_route_markers.py``) passes on the new version.
VERIFIED_FASTAPI_RANGE: tuple[tuple[int, int], tuple[int, int]] = ((0, 136), (0, 137))

#: Route ``__dict__`` key holding the markers pruned from that route.
_PRUNED_ATTR = "_sandloom_pruned_markers"
#: The Dependant fields that must be empty for a sub-dependant to be pruned.
_PARAM_FIELDS = (
    "path_params",
    "query_params",
    "header_params",
    "cookie_params",
    "body_params",
    "dependencies",
)
_PARAM_NAMES = (
    "request_param_name",
    "websocket_param_name",
    "http_connection_param_name",
    "response_param_name",
    "background_tasks_param_name",
    "security_scopes_param_name",
)

_warned_unverified = False


#: Every callable the marker factories produced. Membership — not the
#: ``__sandloom_*__`` attributes, which are metadata only — is what makes a callable
#: prunable: ``functools.wraps(marker)`` around a real guard copies those
#: attributes, and an object with a permissive ``__getattr__`` answers any
#: attribute; neither must ever be mistaken for a no-op and removed.
_REGISTERED: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()

_F = TypeVar("_F", bound=Callable[..., Any])


def register_marker(fn: _F) -> _F:
    """Record ``fn`` as a no-op marker (called by the marker factories only)."""
    _REGISTERED.add(fn)
    return fn


def is_marker(call: Any) -> bool:
    """``True`` only for a callable built by one of the marker factories."""
    if call is None:
        return False
    try:
        return call in _REGISTERED
    except TypeError:  # unhashable / not weak-referenceable: never a marker
        return False


def _dependant_calls(dependant: Any) -> Iterator[Any]:
    for sub in getattr(dependant, "dependencies", None) or ():
        yield getattr(sub, "call", None)
        yield from _dependant_calls(sub)


def route_marker_calls(route: Any) -> Iterator[Any]:
    """Every dependency callable of ``route`` — pruned markers included.

    The single way to read a route's markers: the markers
    :func:`prune_marker_dependencies` removed from the executed dependant,
    then every callable still in it (recursively; ``None`` for a dependency
    without a call). Non-marker callables are yielded too, so the result is
    exactly what walking the unpruned dependant would give.
    """
    yield from (getattr(route, "__dict__", None) or {}).get(_PRUNED_ATTR, ())
    yield from _dependant_calls(getattr(route, "dependant", None))


def _fastapi_version() -> tuple[int, int] | None:
    try:
        major, minor = str(fastapi.__version__).split(".")[:2]
        return int(major), int(minor)
    except ValueError:  # pragma: no cover - defensive
        return None


def pruning_supported() -> bool:
    """Whether the installed FastAPI is inside :data:`VERIFIED_FASTAPI_RANGE`."""
    version = _fastapi_version()
    low, high = VERIFIED_FASTAPI_RANGE
    return version is not None and low <= version < high


def _is_bare(sub: Any) -> bool:
    """A sub-dependant FastAPI would solve to nothing but the call itself.

    ``name is None``: a parameterless ``dependencies=[...]`` entry. A marker
    bound to a PARAMETER of another dependency (``_m: None =
    Depends(marker)``) is named — its value is passed to that call — so it is
    never pruned (it stays executed, and readable).
    """
    if getattr(sub, "name", None) is not None:
        return False
    if any(getattr(sub, name, None) for name in _PARAM_FIELDS):
        return False
    if any(getattr(sub, name, None) is not None for name in _PARAM_NAMES):
        return False
    return not getattr(sub, "own_oauth_scopes", None)


def iter_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Every route of ``routes``, mounted sub-applications' routes included."""
    for route in routes:
        yield route
        nested = getattr(route, "routes", None)
        if nested and getattr(route, "dependant", None) is None:
            yield from iter_routes(nested)


def prune_marker_dependencies(routes: Iterable[Any]) -> int:
    """Stop executing marker dependencies; keep them readable. Idempotent.

    For every HTTP ``APIRoute`` among ``routes`` (mounted sub-applications
    included): move each top-level marker sub-dependency out of
    ``route.dependant.dependencies`` into the route's pruned-marker record.
    Returns how many were pruned. Outside :data:`VERIFIED_FASTAPI_RANGE` it
    prunes nothing and logs a warning (once per process).
    """
    global _warned_unverified
    if not pruning_supported():
        if not _warned_unverified:
            _warned_unverified = True
            low, high = VERIFIED_FASTAPI_RANGE
            logger.warning(
                "FastAPI %s is outside the verified range [%d.%d, %d.%d): route "
                "markers stay executed per request (correct, ~6-8 µs slower per "
                "marker)",
                ".".join(map(str, _fastapi_version() or ("?",))),
                *low,
                *high,
            )
        return 0

    pruned = 0
    for route in iter_routes(routes):
        if not isinstance(route, APIRoute):
            continue
        # typed Any: the shape is re-checked at runtime (fail safe)
        dependant: Any = route.dependant
        subs: Any = getattr(dependant, "dependencies", None)
        if not isinstance(dependant, Dependant) or not isinstance(subs, list):
            continue  # pragma: no cover - unexpected shape: leave it alone
        markers = [
            sub
            for sub in subs
            if isinstance(sub, Dependant) and is_marker(sub.call) and _is_bare(sub)
        ]
        if not markers:
            continue
        record = route.__dict__.get(_PRUNED_ATTR, ())
        route.__dict__[_PRUNED_ATTR] = (*record, *(sub.call for sub in markers))
        drop = {id(sub) for sub in markers}
        subs[:] = [sub for sub in subs if id(sub) not in drop]
        pruned += len(markers)
    return pruned


#: Route attribute naming the module that mounted it (route snapshots).
MODULE_ATTR = "sandloom_module"


def stamp_routes(routes: Iterable[Any], module: str) -> None:
    """Attribute ``routes`` to the module that mounted them.

    Called by the registration draft for the routes a module's routers add;
    routes the kernel mounts carry no stamp (:mod:`sandloom.core.route_snapshots`).
    """
    for route in routes:
        if getattr(route, MODULE_ATTR, None) is None:
            try:
                setattr(route, MODULE_ATTR, module)
            except AttributeError:  # pragma: no cover - a slotted custom route
                continue


def route_module(route: Any) -> str | None:
    """The module that mounted ``route`` (``None``: the kernel did)."""
    module = getattr(route, MODULE_ATTR, None)
    return module if isinstance(module, str) else None


__all__ = [
    "iter_routes",
    "MODULE_ATTR",
    "VERIFIED_FASTAPI_RANGE",
    "is_marker",
    "prune_marker_dependencies",
    "pruning_supported",
    "register_marker",
    "route_marker_calls",
    "route_module",
    "stamp_routes",
]
