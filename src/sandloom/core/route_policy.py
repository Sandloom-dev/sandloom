"""Route policy, model E (ADR-0009; Public API 1.0 spec §4.2, §5.1).

Every API route is DENIED BY DEFAULT: it resolves an authenticator
(``@authenticates`` — a user subject, or ``@authenticates(kind="service")`` —
a system credential) or declares ``public(reason=...)``. ``create_app``
refuses a route that is neither, or both (:class:`RoutePolicyError`,
SANDLOOM-B008). Everything else a route used to declare is DERIVED:

* the effect from the method ("write" = anything but GET / HEAD / OPTIONS —
  it selects defaults and is not a class);
* the session check (live at start on every authenticated route; the
  commit-time re-check on user writes, on a GET whose unit wrote, and by
  ``at_commit()``);
* the risk from its source (framework admin gates, money effects, security
  effects, or the ``sensitive(risk)`` escape hatch);
* idempotency (mode S automatically on keyed user writes; ``replay_safe``
  opts in to body replay; an explicit ``idempotency_guard`` keeps its mode).

The declarations a route may carry are no-op MARKERS (:mod:`sandloom.core.route_markers`
prunes them from the executed dependencies; :func:`route_marker_calls` reads
them): ``public``, ``no_idempotency``, ``replay_safe``, ``sensitive``,
``operation_intent``, ``writes``, ``allow_multi_db_write``. Each returns a
``Depends`` for ``dependencies=[...]``. :func:`resolve_route_policy` is the one
reader; the route snapshot (``routes.snapshot.json``) records its result.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import Depends, params

from sandloom.core.guard_order import authenticator_kind, resolution_order
from sandloom.core.route_markers import iter_routes, register_marker, route_marker_calls
from sandloom.core.unit_of_work.risk import (
    RISK_ATTR,
    RISKS,
    STEP_UP_ATTR,
    STEP_UP_DEFAULTS,
    Risk,
    tag_risk,
)

#: Marker attributes (set by the factories below; membership in the
#: route_markers registry — not the attribute — makes a callable a marker).
PUBLIC_ATTR = "__sandloom_public__"
NO_IDEMPOTENCY_ATTR = "__sandloom_no_idempotency__"
REPLAY_SAFE_ATTR = "__sandloom_replay_safe__"
SENSITIVE_ATTR = "__sandloom_sensitive__"
OPERATION_INTENT_ATTR = "__sandloom_operation_intent__"
WRITES_ATTR = "__sandloom_writes__"
MULTI_DB_WRITE_ATTR = "__sandloom_multi_db_write__"
#: Methods that do not write (everything else selects the write defaults).
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


class RoutePolicyError(RuntimeError):
    """A route's policy declaration is invalid (deny-by-default, SANDLOOM-B008)."""

    sandloom_code = "SANDLOOM-B008"


class StoreCapabilityError(RuntimeError):
    """A write route's store lacks what its defaults need (SANDLOOM-B010), or a
    non-transactional store is injected into it (SANDLOOM-B011)."""

    sandloom_code = "SANDLOOM-B010"


class WriteStoreError(RuntimeError):
    """A write route's write store is ambiguous or its waiver is invalid
    (SANDLOOM-B009 / SANDLOOM-B013)."""

    sandloom_code = "SANDLOOM-B009"


class CapabilityReplayError(RuntimeError):
    """A token-issuer or operation-intent route has the wrong idempotency
    guard, or an intent route takes a request session (SANDLOOM-B019)."""

    sandloom_code = "SANDLOOM-B019"


#: Dependencies that open (or hold) a request unit / read transaction:
#: registered by :mod:`sandloom.core.di` (``DbSession``, ``Database(...)``,
#: ``ReadOnlyDbSession``, ``UowReads``, ``PlannedReads``, ``SnapshotReads``).
_REQUEST_SESSIONS: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()


#: Writable request-session dependencies -> the database they open.
_WRITABLE_SESSIONS: weakref.WeakKeyDictionary[Callable[..., Any], str] = (
    weakref.WeakKeyDictionary()
)


def register_request_session(
    dep: Callable[..., Any], *, writable_database: str | None = None
) -> Callable[..., Any]:
    """Record ``dep`` as a request-session dependency (``sandloom.core.di`` only);
    ``writable_database`` names the database a WRITABLE session opens."""
    _REQUEST_SESSIONS.add(dep)
    if writable_database is not None:
        _WRITABLE_SESSIONS[dep] = writable_database
    return dep


def writable_session_database(call: Any) -> str | None:
    """The database a writable request-session dependency opens, else ``None``."""
    try:
        return _WRITABLE_SESSIONS.get(call) if call is not None else None
    except TypeError:
        return None


def is_request_session(call: Any) -> bool:
    try:
        return call is not None and call in _REQUEST_SESSIONS
    except TypeError:
        return False


@dataclass(frozen=True, slots=True)
class PublicDecl:
    """``public(reason=..., issues_tokens=...)``."""

    reason: str
    issues_tokens: bool


@dataclass(frozen=True, slots=True)
class MultiDbWriteDecl:
    """``allow_multi_db_write(secondaries=..., reason=...)``."""

    secondaries: tuple[str, ...]
    reason: str


def _reason(reason: object, what: str, example: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise RoutePolicyError(
            f"{what}() needs a reason: "
            f'Depends({what}(reason="{example}")) (SANDLOOM-B008)'
        )
    return reason.strip()


def _marker(name: str, attr: str, value: object) -> params.Depends:
    async def _declaration() -> None:  # async: no threadpool hop
        return None

    setattr(_declaration, attr, value)
    _declaration.__name__ = name
    _declaration.__qualname__ = name
    return cast(params.Depends, Depends(register_marker(_declaration)))


def public(*, reason: str, issues_tokens: bool = False) -> params.Depends:
    """Declare a route unauthenticated, and why (ADR-0009 item 1).

    ``dependencies=[public(reason="liveness probe")]``. A token issuer (login,
    refresh, SSO callback) adds ``issues_tokens=True``: its live issuance
    check is unchanged, and an explicit idempotency guard on it must be the
    release-and-re-execute form (SANDLOOM-B019). A PSP webhook is public too; its
    verified signature, checked in the endpoint, is its authorization.
    """
    decl = PublicDecl(
        _reason(reason, "public", "why this route has no caller"), bool(issues_tokens)
    )
    return _marker("public", PUBLIC_ATTR, decl)


def no_idempotency(*, reason: str) -> params.Depends:
    """Opt a write route out of the automatic idempotency guard (streaming,
    non-JSON bodies). The reason is recorded in the route snapshot."""
    return _marker(
        "no_idempotency",
        NO_IDEMPOTENCY_ATTR,
        _reason(reason, "no_idempotency", "why a retry must re-run this route"),
    )


def replay_safe(*, reason: str) -> params.Depends:
    """Opt a keyed write in to BODY replay (idempotency mode B, spec §4.7).

    Only when the response contains nothing whose visibility could change
    between the attempts (typically a creation echoing the caller's own
    fields and the new id) — never a secret, a bearer capability, other users'
    data or role / ownership dependent fields. The reason is reviewed in the
    route snapshot. Without it the automatic guard replays status only.
    """
    return _marker(
        "replay_safe",
        REPLAY_SAFE_ATTR,
        _reason(reason, "replay_safe", "the body echoes only the caller's own input"),
    )


def sensitive(risk: Risk) -> params.Depends:
    """The escape hatch for a route whose authority check is hand-rolled:
    declare its risk (``admin`` / ``money`` / ``security``) so the framework
    applies what that risk triggers (ADR-0009 item 9)."""
    if risk not in RISKS:
        raise RoutePolicyError(
            f"sensitive() risk must be one of {', '.join(RISKS)}, not {risk!r}"
        )
    return _marker("sensitive", SENSITIVE_ATTR, risk)


def operation_intent(*, reason: str) -> params.Depends:
    """Declare the operation-intent mode (spec §4.6): no request unit,
    sequential ``open_uow()`` units, external calls in ``external_call()``,
    and ``release_check()`` before a 2xx (SANDLOOM-R008 / SANDLOOM-R012). Only for
    routes that call an external provider (billing checkout and portal)."""
    return _marker(
        "operation_intent",
        OPERATION_INTENT_ATTR,
        _reason(reason, "operation_intent", "PSP session issued under an intent"),
    )


def writes(database: str) -> params.Depends:
    """Name the route's write store when it injects several writable sessions
    (spec §6.1, SANDLOOM-B009)."""
    if not isinstance(database, str) or not database.strip():
        raise RoutePolicyError('writes() needs a database name: writes("default")')
    return _marker("writes", WRITES_ATTR, database.strip())


def allow_multi_db_write(
    *, secondaries: tuple[str, ...], reason: str
) -> params.Depends:
    """The reviewed best-effort 1PC opt-out of the one-writer rule (spec §4.4
    rule 4): the listed secondaries may write too; they commit first and the
    write store last."""
    names = tuple(str(s).strip() for s in secondaries or ())
    if not names or any(not n for n in names):
        raise RoutePolicyError(
            "allow_multi_db_write() needs the secondary databases "
            "it admits (SANDLOOM-B013)"
        )
    decl = MultiDbWriteDecl(
        names, _reason(reason, "allow_multi_db_write", "why the 1PC risk is accepted")
    )
    return _marker("allow_multi_db_write", MULTI_DB_WRITE_ATTR, decl)


# --------------------------------------------------------------------------- #
# The resolved policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RoutePolicy:
    """The static, resolved model E policy of one route (ADR-0009 item 14).

    ``auth``: ``"user"`` / ``"service"`` / ``"public"``; ``authenticators``
    names them (qualnames). ``effect``: ``"read"`` / ``"write"`` by method.
    ``risk``: ``{risk: (source, ...)}`` of the static tags.
    """

    method: str
    path: str
    auth: str
    authenticators: tuple[str, ...] = ()
    public: PublicDecl | None = None
    effect: str = "read"
    risk: dict[str, tuple[str, ...]] = field(default_factory=dict)
    no_idempotency: str | None = None
    replay_safe: str | None = None
    operation_intent: str | None = None
    writes: str | None = None
    multi_db_write: MultiDbWriteDecl | None = None
    #: the step-up max age (seconds) a static tag derives, else ``None``
    step_up_max_age_s: int | None = None

    @property
    def is_user(self) -> bool:
        return self.auth == "user"

    @property
    def is_write(self) -> bool:
        return self.effect == "write"


def _qualname(call: Any) -> str:
    module = getattr(call, "__module__", "") or ""
    name = getattr(call, "__qualname__", None) or getattr(call, "__name__", repr(call))
    return f"{module}.{name}" if module else str(name)


def _declared(route: Any) -> Iterator[tuple[str, object]]:
    attrs = (
        PUBLIC_ATTR,
        NO_IDEMPOTENCY_ATTR,
        REPLAY_SAFE_ATTR,
        SENSITIVE_ATTR,
        OPERATION_INTENT_ATTR,
        WRITES_ATTR,
        MULTI_DB_WRITE_ATTR,
    )
    for call in route_marker_calls(route):
        if call is None:
            continue
        for attr in attrs:
            value = getattr(call, attr, None)
            if value is not None:
                yield attr, value


def _scan_dependencies(
    route: Any,
    kinds: dict[str, set[str]],
    risk: dict[str, list[str]],
    step_ups: list[int],
) -> None:
    """Authenticators and risk-tagged dependencies of ``route``."""
    for call in resolution_order(route.dependant):
        kind = authenticator_kind(call)
        if kind is not None:
            kinds.setdefault(kind, set()).add(_qualname(call))
        tagged = getattr(call, RISK_ATTR, None) if call is not None else None
        if tagged in RISKS:
            risk.setdefault(tagged, []).append(_qualname(call))
            step = getattr(call, STEP_UP_ATTR, None)
            if isinstance(step, int):
                step_ups.append(step)


def resolve_route_policy(route: Any, method: str | None = None) -> RoutePolicy:
    """The resolved policy of ``route`` (one method; the first by default)."""
    methods = sorted(getattr(route, "methods", None) or ("GET",))
    method = method or methods[0]
    kinds: dict[str, set[str]] = {}
    risk: dict[str, list[str]] = {}
    step_ups: list[int] = []
    _scan_dependencies(route, kinds, risk, step_ups)
    decls: dict[str, list[object]] = {}
    for attr, value in _declared(route):
        decls.setdefault(attr, []).append(value)
    for value in decls.get(SENSITIVE_ATTR, ()):
        risk.setdefault(str(value), []).append("sensitive()")
        derived = STEP_UP_DEFAULTS.get(str(value))
        if derived is not None:
            step_ups.append(derived)
    public_decl = next(iter(decls.get(PUBLIC_ATTR, ())), None)
    if "user" in kinds:
        auth = "user"
    elif "service" in kinds:
        auth = "service"
    else:
        auth = "public"
    authenticators = tuple(sorted(set().union(*kinds.values()))) if kinds else ()

    def _one(attr: str) -> Any:
        values = decls.get(attr, ())
        return values[0] if values else None

    return RoutePolicy(
        method=method,
        path=str(getattr(route, "path", "?")),
        auth=auth,
        authenticators=authenticators,
        public=public_decl if isinstance(public_decl, PublicDecl) else None,
        effect="read" if method in READ_METHODS else "write",
        risk={k: tuple(sorted(set(v))) for k, v in sorted(risk.items())},
        no_idempotency=_one(NO_IDEMPOTENCY_ATTR),
        replay_safe=_one(REPLAY_SAFE_ATTR),
        operation_intent=_one(OPERATION_INTENT_ATTR),
        writes=_one(WRITES_ATTR),
        multi_db_write=_one(MULTI_DB_WRITE_ATTR),
        step_up_max_age_s=min(step_ups) if step_ups else None,
    )


def api_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """The HTTP API routes (a FastAPI dependant and methods) of ``routes``,
    mounted sub-applications included. Websocket routes and the kernel's
    plain routes (``/_sandloom/rpc``, OpenAPI / docs) are not API routes."""
    for route in iter_routes(routes):
        if getattr(route, "dependant", None) is not None and getattr(
            route, "methods", None
        ):
            yield route


def route_policy_problems(routes: Iterable[Any]) -> list[str]:
    """Every deny-by-default violation among ``routes`` (SANDLOOM-B008)."""
    problems: list[str] = []
    for route in api_routes(routes):
        policy = resolve_route_policy(route)
        label = f"{','.join(sorted(route.methods))} {route.path}"
        publics = [v for a, v in _declared(route) if a == PUBLIC_ATTR]
        if policy.auth != "public" and publics:
            problems.append(
                f"{label}: declares public() and authenticates "
                f"({', '.join(policy.authenticators)}). Fix: remove one."
            )
        elif policy.auth == "public" and not publics:
            problems.append(
                f"{label}: no authenticator and not public. Fix: add a user "
                "dependency (user: CurrentUser) or a gate, or "
                'Depends(public(reason="...")) if the route is meant to be '
                "public (ADR-0009)."
            )
        elif len(publics) > 1 and len(set(publics)) > 1:
            problems.append(
                f"{label}: declares public() more than once with different "
                "reasons. Fix: keep one."
            )
    return problems


def _writable_databases(route: Any) -> list[str]:
    seen: list[str] = []
    for call in resolution_order(route.dependant):
        db = writable_session_database(call)
        if db is not None and db not in seen:
            seen.append(db)
    return seen


def _write_store_problems(label: str, policy: RoutePolicy, dbs: list[str]) -> list[str]:
    problems: list[str] = []
    waiver = policy.multi_db_write
    if policy.writes is not None and dbs and policy.writes not in dbs:
        problems.append(
            f"{label}: writes({policy.writes!r}) names a database the route "
            f"does not inject (it injects {', '.join(dbs)}) (SANDLOOM-B009)"
        )
    elif len(dbs) > 1 and policy.writes is None and waiver is None:
        problems.append(
            f"{label}: several writable sessions ({', '.join(dbs)}) and no write "
            'store: declare writes("<database>") (the others are read in it), '
            "or allow_multi_db_write(secondaries=..., reason=...) (SANDLOOM-B009)"
        )
    if waiver is not None:
        store = policy.writes or (dbs[0] if dbs else "default")
        problems.extend(
            f"{label}: allow_multi_db_write secondary {secondary!r} is the write "
            "store or not injected (SANDLOOM-B013)"
            for secondary in waiver.secondaries
            if secondary == store or (dbs and secondary not in dbs)
        )
    return problems


def writable_databases(route: Any) -> list[str]:
    """The databases the route's writable request sessions open, in
    resolution order (``DbSession`` is ``default``)."""
    return _writable_databases(route)


def write_store_problems(routes: Iterable[Any]) -> list[str]:
    """Write routes whose write store is ambiguous (SANDLOOM-B009: several writable
    sessions and neither ``writes()`` nor ``allow_multi_db_write``, or
    ``writes()`` naming a database the route does not inject) or whose
    ``allow_multi_db_write`` is invalid (SANDLOOM-B013: a secondary the route does
    not inject, or the write store itself)."""
    problems: list[str] = []
    for route in api_routes(routes):
        writes = [
            policy
            for policy in (
                resolve_route_policy(route, m) for m in sorted(route.methods)
            )
            if policy.is_write and policy.operation_intent is None
        ]
        if writes:
            policy = writes[0]
            problems += _write_store_problems(
                f"{policy.method} {route.path}", policy, _writable_databases(route)
            )
    return problems


def validate_write_stores(routes: Iterable[Any]) -> None:
    """Startup check: raise :class:`WriteStoreError` listing every problem."""
    problems = write_store_problems(routes)
    if problems:
        raise WriteStoreError("; ".join(problems))


def validate_route_policy(routes: Iterable[Any]) -> None:
    """Startup check (``create_app``): raise :class:`RoutePolicyError`
    listing every route that is neither authenticated nor public, or both."""
    problems = route_policy_problems(routes)
    if problems:
        raise RoutePolicyError(
            "; ".join(problems) + " (sandloom explain SANDLOOM-B008)"
        )


__all__ = [
    "CapabilityReplayError",
    "StoreCapabilityError",
    "writable_databases",
    "WriteStoreError",
    "validate_write_stores",
    "write_store_problems",
    "writable_session_database",
    "is_request_session",
    "register_request_session",
    "MULTI_DB_WRITE_ATTR",
    "NO_IDEMPOTENCY_ATTR",
    "OPERATION_INTENT_ATTR",
    "PUBLIC_ATTR",
    "READ_METHODS",
    "REPLAY_SAFE_ATTR",
    "RISKS",
    "RISK_ATTR",
    "SENSITIVE_ATTR",
    "STEP_UP_ATTR",
    "WRITES_ATTR",
    "MultiDbWriteDecl",
    "PublicDecl",
    "Risk",
    "RoutePolicy",
    "RoutePolicyError",
    "allow_multi_db_write",
    "api_routes",
    "no_idempotency",
    "operation_intent",
    "public",
    "replay_safe",
    "resolve_route_policy",
    "route_policy_problems",
    "sensitive",
    "tag_risk",
    "validate_route_policy",
    "writes",
]
