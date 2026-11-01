"""The per-route runtime of model E (spec §4.2-§4.6, ADR-0009).

``create_app`` wraps every API route's request handler once
(:func:`install_route_runtime`; a plain function wrapper around the handler
FastAPI built — not a dependency, which would cost a dependency solve per
request). Per request it:

* binds a :class:`~sandloom.core.unit_of_work.request.RequestScope`: the handler
  task (the owner of the request unit), the route's resolved policy, the
  request's write admission (one writer per request, spec §4.4) and the lazy
  request unit ``db()`` creates on first use;
* arms the commit-time re-check (spec §4.5) on authenticated user writes; a
  read arms it per unit that wrote, ``at_commit()`` for every unit;
* commits the lazy unit when the handler succeeds (rolls it back on any
  exception) BEFORE the response is sent (G-UOW-02);
* hands the response to a durable idempotency claim before its unit commits
  (explicit durable guards and the automatic guard).

``Cache-Control: no-store`` (ADR-0009 item 4) is the security-headers
middleware's: every response without its own ``Cache-Control`` gets it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from functools import partial
from typing import Any, cast

from fastapi import Depends, Request, Response, params
from fastapi.exceptions import FastAPIError
from starlette._exception_handler import wrap_app_handling_exceptions
from starlette.types import Receive, Scope, Send

import sandloom.core.auth.bearer as _bearer
import sandloom.core.auth.verification as _verification
import sandloom.core.projections.operations as _projection_ops
import sandloom.core.projections.tokens as _tokens
import sandloom.core.unit_of_work as _uow
import sandloom.core.unit_of_work.intent as _intent
import sandloom.core.unit_of_work.request as _request
import sandloom.core.unit_of_work.risk as _risk
import sandloom.core.unit_of_work.scope as _uow_scope
from sandloom.core.contract_context import _read_scope_var
from sandloom.core.guard_order import resolution_order
from sandloom.core.idempotency import automatic as _automatic
from sandloom.core.idempotency import durable as _durable
from sandloom.core.route_policy import (
    CapabilityReplayError,
    RoutePolicy,
    StoreCapabilityError,
    api_routes,
    is_request_session,
    resolve_route_policy,
    writable_databases,
    writable_session_database,
)

#: Route ``__dict__`` key: the runtime wrapper is installed.
_RUNTIME_ATTR = "_sandloom_route_runtime"

_logger = logging.getLogger("sandloom.core.route_runtime")

_scope_var = _request._request_scope
_admission_var = _request._admission


def _policies(route: Any) -> dict[str, RoutePolicy]:
    return {m: resolve_route_policy(route, m) for m in route.methods or ("GET",)}


def _intent_state() -> _intent.IntentState:
    state = _intent.IntentState()
    read_scope = _read_scope_var.get()
    release = getattr(read_scope, "release_connections", None)
    if release is not None:
        state.read_scope_release = release
    return state


class _Plan:
    """What the runtime needs per route and method, resolved once at
    startup (the per-request work is one allocation and two context
    variables)."""

    __slots__ = (
        "armed",
        "intent",
        "lazy_factory",
        "policy",
        "secondaries",
        "sequential",
        "step_up",
        "tokens",
        "user",
        "write_store",
    )

    def __init__(self, core: Any, policy: RoutePolicy) -> None:
        self.policy = policy
        self.write_store = policy.writes
        self.secondaries = frozenset(
            policy.multi_db_write.secondaries
            if policy.multi_db_write is not None
            else ()
        )
        self.intent = policy.operation_intent is not None
        self.sequential = self.intent
        self.lazy_factory = (
            None if self.intent else partial(_uow.LazyUnit, core, "default")
        )
        self.user = policy.auth == "user"
        self.armed = self.user and policy.effect == "write" and not self.intent
        self.step_up = policy.step_up_max_age_s if self.user else None
        # the projections a write's events feed: its consistency token
        registry = getattr(core, "projection_registry", None)
        self.tokens = (
            registry.token_sources() or None
            if registry is not None and policy.effect == "write"
            else None
        )


def _check_release(
    scope: _request.RequestScope,
    request: Request,
    policy: RoutePolicy,
    response: Response,
) -> None:
    intent = scope.intent
    if intent is not None and 200 <= response.status_code < 300:
        problem = intent.release_problem()
        if problem is not None:
            # the body (a bearer value) never leaves
            raise _intent.IntentReleaseError(
                f"{request.method} {policy.path} answered "
                f"{response.status_code} outside the release rule: "
                f"{problem} (SANDLOOM-R012)"
            )


def _begin(core: Any, plan: _Plan, request: Request) -> _request.RequestScope:
    """The request's scope (one allocation; the plan's inputs precomputed)."""
    scope = _request.RequestScope.for_route(
        core,
        plan.policy,
        request,
        plan.write_store,
        plan.secondaries,
        plan.sequential,
        plan.lazy_factory,
        asyncio.current_task(),
    )
    if plan.intent:
        scope.intent = _intent_state()
    if plan.user:
        scope.armed = plan.armed
        _bind_subject(scope, request)
        if plan.step_up is not None:
            _risk.require_step_up(scope, plan.step_up)
    return scope


def _handled(
    scope: _request.RequestScope, asgi: Scope, response: Response, capturing: bool
) -> None:
    """After the handler returned, before the unit commits."""
    if capturing:
        pending = asgi["state"].get(_durable._DURABLE_ATTR)
        if pending is not None:
            # the durable record completes with the response before the unit
            # commits (explicit and automatic guards)
            pending.response = response
    risk = scope.risk
    if risk is not None:
        if risk.step_up:
            # a static step-up (sensitive("security")) the handler did not
            # satisfy: the response never leaves (RFC 9470 401)
            _risk.settle_step_up(scope)
        if (
            risk.elsewhere
            and scope.lazy_factory is not None
            and getattr(scope.lazy, "session", None) is None
            and _risk.needs_audit_unit(scope)
        ):
            # a risky command committed in its own unit and the handler
            # opened no unit: open the request unit for the mandatory audit
            # row (its commit records it, G-SENS-04)
            _request.open_request_unit(scope)


def _issue_token(plan: _Plan, scope: _request.RequestScope, response: Response) -> None:
    """``SANDLOOM-Consistency-Token`` of a write feeding projections (spec §6.4
    item 7), from the events pending in the request's units."""
    sessions = dict.fromkeys(_uow_scope._active_uow_sessions.get())
    lazy_session = getattr(scope.lazy, "session", None)
    if lazy_session is not None:
        sessions[lazy_session] = None
    token = _projection_ops.response_token(cast(Any, plan.tokens), sessions)
    if token is not None:
        response.headers[_tokens.CONSISTENCY_HEADER] = token


def _end(scope: _request.RequestScope, plan: _Plan) -> None:
    """The request is over: report its risk effects (G-ROUTE-05) and break
    its reference cycle (scope -> request -> ASGI scope -> dependency exit
    stacks -> session -> session.info -> scope), so its object graph is freed
    by reference counting, not by the cyclic collector."""
    if _risk.effect_observer is not None and scope.risk is not None:
        _observe(plan.policy, scope.risk)
    scope.request = None
    scope.lazy = None


def _runtime_app(
    handler: Callable[[Request], Awaitable[Response]],
    policies: dict[str, RoutePolicy],
    core: Any,
    capturing: bool,
) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """The route's ASGI app: FastAPI's ``request_response`` with the route
    runtime inlined in its inner app — one coroutine frame per request less
    than wrapping the handler (every suspension of the handler resumes
    through each frame).

    The FastAPI part is ``fastapi.routing.request_response`` (the request
    and function exit stacks, the "response not awaited" guard, the
    exception handlers); the runtime part brackets ``handler(request)``.
    """
    plans = {method: _Plan(core, policy) for method, policy in policies.items()}
    fallback = next(iter(plans.values()))
    scope_set, scope_reset = _scope_var.set, _scope_var.reset
    admission_set, admission_reset = _admission_var.set, _admission_var.reset

    async def app(asgi: Scope, receive: Receive, send: Send) -> None:
        request = Request(asgi, receive, send)

        async def inner(asgi: Scope, receive: Receive, send: Send) -> None:
            response_awaited = False
            async with AsyncExitStack() as request_stack:
                asgi["fastapi_inner_astack"] = request_stack
                plan = plans.get(asgi["method"], fallback)
                scope = _begin(core, plan, request)
                scope_token = scope_set(scope)
                admission_token = admission_set(scope)
                try:
                    # DbSession's function-scoped dependency commits while this
                    # stack unwinds. Keep risk and write admission bound through
                    # that commit, before resetting them or sending the response.
                    async with AsyncExitStack() as function_stack:
                        asgi["fastapi_function_astack"] = function_stack
                        try:
                            response = await handler(request)
                            _handled(scope, asgi, response, capturing)
                            if plan.tokens is not None:
                                _issue_token(plan, scope, response)
                        except BaseException as exc:
                            if scope.lazy is not None:
                                await scope.lazy.finish(exc)
                            raise
                        if scope.lazy is not None:
                            await scope.lazy.finish(None)
                        if scope.intent is not None:
                            _check_release(scope, request, plan.policy, response)
                finally:
                    admission_reset(admission_token)
                    scope_reset(scope_token)
                    _end(scope, plan)
                await response(asgi, receive, send)
                response_awaited = True
            if not response_awaited:
                raise FastAPIError(
                    "Response not awaited. There's a high chance that the "
                    "application code is raising an exception and a dependency "
                    "with yield has a block with a bare except, or a block with "
                    "except Exception, and is not raising the exception again."
                )

        await wrap_app_handling_exceptions(inner, request)(asgi, receive, send)

    return app


def _observe(policy: RoutePolicy, risk: Any) -> None:
    """Hand the request's risk effects to the test harness's observer
    (G-ROUTE-05, informational: a failing observer never fails a request)."""
    observer = _risk.effect_observer
    if observer is None or not risk.risks:
        return
    try:
        observer(policy.method, policy.path, risk.risks)
    except Exception:
        _logger.debug("effect observer failed", exc_info=True)


def _bind_subject(scope: _request.RequestScope, request: Any) -> None:
    """The request's subject and the live check of its session (the bearer is
    verified once per request: the authenticator reuses the same decode)."""
    claims = _bearer.bearer_claims(request)
    if claims is None:
        return
    scope.subject = str(claims.get("sub"))
    scope.claims = claims
    scope.check = partial(_verification.assert_token_valid_now, claims)


def at_commit() -> params.Depends:
    """Re-check the caller's session before the COMMIT of EVERY unit the
    request opens, empty ones included (spec §4.5; today's
    ``revocation_at_commit()``). Writes are re-checked automatically; this is
    for a non-write route with an explicit commit obligation."""

    async def _arm_at_commit(request: Request) -> None:
        scope = _request.current_request_scope()
        if scope is None:
            # A route served outside the route runtime (a bare test app): the
            # scope is bound for the rest of this request's context.
            scope = _request.RequestScope(
                core=getattr(getattr(request.app, "state", None), "core", None),
                policy=None,
                request=request,
            )
            _scope_var.set(scope)
            for session in _request.active_units():
                info = getattr(session, "info", None)
                if isinstance(info, dict) and info.get(_request.SCOPE_KEY) is None:
                    info[_request.SCOPE_KEY] = scope  # opened before this dep
        scope.at_commit = True
        if scope.check is None:
            _bind_subject(scope, request)

    _arm_at_commit.__sandloom_at_commit__ = True  # type: ignore[attr-defined]
    return cast(params.Depends, Depends(_arm_at_commit))


def capability_replay_problems(routes: Iterable[Any]) -> list[str]:
    """SANDLOOM-B019 (spec §4.6, §4.7): a token issuer's guard, and an intent
    route's REQUIRED guard, must release and re-execute (mode X:
    ``idempotency_guard(scope=..., fail_closed=True, store_response=False)``);
    an intent route takes no request-session dependency."""
    problems: list[str] = []
    for route in api_routes(routes):
        policy = resolve_route_policy(route)
        issuer = policy.public is not None and policy.public.issues_tokens
        intent = policy.operation_intent is not None
        if not (issuer or intent):
            continue
        label = f"{','.join(sorted(route.methods))} {route.path}"
        modes = list(_durable._guard_modes(route))
        if any(m.kind != "in_flight" or not m.fail_closed for m in modes):
            problems.append(
                f"{label}: a {'token issuer' if issuer else 'operation-intent route'} "
                "may only carry the release-and-re-execute guard "
                "idempotency_guard(scope=..., fail_closed=True, store_response=False)"
            )
        if intent and not modes:
            problems.append(
                f"{label}: an operation-intent route needs the explicit guard "
                "idempotency_guard(scope=..., fail_closed=True, store_response=False)"
            )
        if intent and any(
            is_request_session(c) for c in resolution_order(route.dependant)
        ):
            problems.append(
                f"{label}: an operation-intent route takes no request session "
                "(DbSession / Database(...) / planned reads): open short units "
                "with db_session()"
            )
    return problems


def validate_capability_replay(routes: Iterable[Any]) -> None:
    problems = capability_replay_problems(routes)
    if problems:
        raise CapabilityReplayError(
            "; ".join(problems) + " (sandloom explain SANDLOOM-B019)"
        )


#: Features the write store must advertise for a route's defaults (spec
#: §6.1): the mandatory audit row of a sensitive write, the durable record of
#: an explicit durable idempotency guard. (The automatic guard falls back to
#: the fail-closed in-flight form without a durable log, G-IDEM-18; an event
#: published to a store without an outbox is refused where it happens.)
_AUDIT_FEATURE = "audit"
_DURABLE_FEATURE = "http_idempotency"
_TRANSACTIONS_FEATURE = "transactions"
_ADMISSION_FEATURE = "write_admission"


def store_capability_problems(
    routes: Iterable[Any],
    advertised: Callable[[str], frozenset[str] | None],
) -> list[str]:
    """SANDLOOM-B010 / SANDLOOM-B011 (spec §6.1, §6.2) for every write route whose
    defaults apply (not an operation intent): ``advertised(database)`` is the
    database's build-free feature set (``None``: not configured, skipped).

    * B011: a writable session on a store without ``transactions`` (other
      than a declared ``allow_multi_db_write`` secondary, the reviewed
      opt-out) cannot commit with the request unit;
    * B010: the write store lacks ``audit`` on a route with a static risk
      (the mandatory audit row), or ``http_idempotency`` on a route with an
      explicit durable idempotency guard.
    """
    problems: list[str] = []
    hosts: dict[str, str] = {}  # database -> the first route it hosts units of
    for route in api_routes(routes):
        dbs = writable_databases(route)
        durable = any(m.kind == "durable" for m in _durable._guard_modes(route))
        for database in dict.fromkeys(["default", *dbs]):
            hosts.setdefault(
                database, f"{','.join(sorted(route.methods))} {route.path}"
            )
        for method in sorted(route.methods or ("GET",)):
            policy = resolve_route_policy(route, method)
            if policy.is_write and policy.operation_intent is None:
                label = f"{method} {route.path}"
                problems += _transaction_problems(label, policy, dbs, advertised)
                problems += _feature_problems(label, policy, dbs, durable, advertised)
    for database, route_label in hosts.items():
        feats = advertised(database)
        if feats is not None and _ADMISSION_FEATURE not in feats:
            problems.append(
                f"database {database!r} hosts request units ({route_label}, ...) "
                f"but its driver lacks the {_ADMISSION_FEATURE!r} feature (one "
                "writer per request, write detection, closed-unit refusal, the "
                "lazy unit): use a driver that implements it (SANDLOOM-B010)"
            )
    return problems


def _transaction_problems(
    label: str,
    policy: RoutePolicy,
    dbs: list[str],
    advertised: Callable[[str], frozenset[str] | None],
) -> list[str]:
    store = policy.writes or (dbs[0] if dbs else "default")
    waived = set(policy.multi_db_write.secondaries) if policy.multi_db_write else set()
    problems: list[str] = []
    for database in dict.fromkeys([store, *dbs]):
        feats = advertised(database)
        if feats is None or _TRANSACTIONS_FEATURE in feats or database in waived:
            continue
        problems.append(
            f"{label}: database {database!r} is not transactional (no "
            f"{_TRANSACTIONS_FEATURE!r} feature), so its writes cannot "
            "commit with the request unit and carry none of the route's "
            "guarantees: write through a transactional write store and "
            "project into it, or declare it in allow_multi_db_write("
            "secondaries=..., reason=...) (SANDLOOM-B011)"
        )
    return problems


def _feature_problems(
    label: str,
    policy: RoutePolicy,
    dbs: list[str],
    durable: bool,
    advertised: Callable[[str], frozenset[str] | None],
) -> list[str]:
    store = policy.writes or (dbs[0] if dbs else "default")
    # (database, feature, what needs it); the explicit durable guard
    # records in the default database's request log
    needs: list[tuple[str, str, str]] = []
    if policy.risk:
        needs.append(
            (store, _AUDIT_FEATURE, "the mandatory audit row of a sensitive write")
        )
    if durable:
        needs.append(
            ("default", _DURABLE_FEATURE, "its durable idempotency_guard record")
        )
    problems: list[str] = []
    for database, feature, why in needs:
        feats = advertised(database)
        if feats is None or feature in feats:
            continue
        problems.append(
            f"{label}: database {database!r} lacks the {feature!r} "
            f"feature {why} needs: configure a database whose driver "
            "provides it, or drop the default that needs it (SANDLOOM-B010)"
        )
    return problems


def validate_store_capabilities(routes: Iterable[Any], registry: Any) -> None:
    """Startup check (``create_app``): raise :class:`StoreCapabilityError`
    listing every SANDLOOM-B010 / SANDLOOM-B011 problem against ``registry``'s
    database instances (no-op without a registry)."""
    if registry is None or not hasattr(registry, "advertised_features"):
        return
    problems = store_capability_problems(
        routes, lambda name: registry.advertised_features("database", name)
    )
    if problems:
        raise StoreCapabilityError("; ".join(problems))


def _session_databases(dep: Any) -> set[str]:
    """The writable databases ``dep`` (and its sub-dependencies) open."""
    calls = [dep.call, *resolution_order(dep)]
    return {db for c in calls if (db := writable_session_database(c)) is not None}


def _store_session_first(node: Any, secondaries: frozenset[str]) -> None:
    """Solve the write store's session before every secondary's session in
    ``node``'s whole dependency tree.

    At each level, every dependency whose subtree opens the write store moves
    (in its order) before the first dependency that opens only secondaries;
    then each dependency opening both is ordered the same way, recursively.
    In FastAPI's depth-first solve (a cached dependency solved once), the
    first secondary session is then never solved before the write store's:
    where their paths part, the secondary's branch opens no write store, so
    it comes after the store's branch; where they do not part, the branch
    opens both and is ordered itself."""
    deps = node.dependencies
    roles = [_session_databases(dep) for dep in deps]
    first = next(
        (i for i, r in enumerate(roles) if r & secondaries and not r - secondaries),
        None,
    )
    if first is not None:
        moved = [i for i in range(first + 1, len(deps)) if roles[i] - secondaries]
        if moved:
            order = [*range(first), *moved]
            order += [i for i in range(first, len(deps)) if i not in moved]
            deps[:] = [deps[i] for i in order]
            roles = [roles[i] for i in order]
    for dep, role in zip(deps, roles, strict=True):
        if role & secondaries and role - secondaries:
            _store_session_first(dep, secondaries)


def _secondaries_commit_first(route: Any, policies: dict[str, RoutePolicy]) -> None:
    """``allow_multi_db_write``: the secondaries commit first, the write store
    last (docs/modules/guarantees/write-admission.md rule 4), whatever the endpoint's
    parameter order. Session dependencies commit when FastAPI unwinds them,
    last solved first: the write store's session is moved to be solved
    before every secondary's (startup only; values bind by name, so the
    endpoint is unaffected). db()'s lazy unit never meets a secondary
    session: while a ``Database(...)`` session is open, ``db()`` is that
    innermost unit."""
    secondaries = frozenset(
        s
        for policy in policies.values()
        if policy.multi_db_write is not None
        for s in policy.multi_db_write.secondaries
    )
    if secondaries:
        _store_session_first(route.dependant, secondaries)


def install_route_runtime(routes: Iterable[Any], core: Any) -> int:
    """Wrap every API route's handler once (idempotent); returns how many."""
    wrapped = 0
    for route in api_routes(routes):
        if route.__dict__.get(_RUNTIME_ATTR):
            continue
        policies = _policies(route)
        _secondaries_commit_first(route, policies)
        guard_modes = list(_durable._guard_modes(route))
        auto = {
            method: mode
            for method, policy in policies.items()
            if (mode := _automatic.automatic_mode(policy, bool(guard_modes)))
        }
        installed = _automatic.install_automatic_guard(route, auto)
        handler = route.get_route_handler()
        if installed or any(m.kind == "durable" for m in guard_modes):
            # the runtime wrapper hands the response to the claim
            route.__dict__[_durable._CAPTURING_ATTR] = True
        capturing = bool(route.__dict__.get(_durable._CAPTURING_ATTR))
        route.app = _runtime_app(handler, policies, core, capturing)
        route.__dict__[_RUNTIME_ATTR] = True
        wrapped += 1
    return wrapped


__all__ = [
    "at_commit",
    "capability_replay_problems",
    "install_route_runtime",
    "store_capability_problems",
    "validate_capability_replay",
    "validate_store_capabilities",
]
