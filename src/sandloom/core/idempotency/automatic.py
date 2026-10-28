"""The automatic idempotency guard of model E (ADR-0009 item 5, spec §4.7).

Every authenticated user WRITE that carries ``Idempotency-Key`` and declares
no explicit ``idempotency_guard``, no ``no_idempotency(reason=)`` and no
operation intent is deduped by the framework:

* **mode S** (status replay), always — a retry after success gets the stored
  status, an empty body and ``Idempotent-Replay: true``; it never enters the
  handler and never returns a body, so a changed authorization cannot leak
  one (spec §4.7, owner decision 2);
* **mode B** (body replay) only where the route declares
  ``replay_safe(reason=)`` and carries no risk (a sensitive write is S);
* the record is the durable request log of the route's REQUEST UNIT (the
  session the route injected: ``DbSession`` / ``Database("x")``), claimed
  in that unit and completed right before its COMMIT, so it commits iff the
  effect commits (ADR-0005);
* a route WITHOUT a request unit gets the fail-closed in-flight form
  (G-IDEM-18): a concurrent duplicate is ``409``, a cache outage ``503``,
  nothing is stored, a later retry runs again.

The decision runs after every route-level dependency (the live session
check, tenant binding, gates and a static step-up, G-IDEM-15): the guard
wraps the ENDPOINT call (installed once by the route runtime, no dependency
solved per request), and a request without the header pays one header read.
"""

from __future__ import annotations

import asyncio
import asyncio.coroutines
import functools
import inspect
from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool

import sandloom.core.unit_of_work.request as _request
import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import effects
from sandloom.core.idempotency.durable import claim_durable
from sandloom.core.idempotency.fingerprint import _DEFAULT_TTL_SECONDS, _request_key
from sandloom.core.idempotency.guard import _cache_guard
from sandloom.core.unit_of_work.scope import _DB_NAME_KEY

#: Route ``__dict__`` key: ``{method: mode}`` of the automatic guard.
AUTO_ATTR = "_sandloom_automatic_idempotency"
_TTL = effects.ttl_or_default(_DEFAULT_TTL_SECONDS)


def automatic_mode(policy: Any, explicit_guard: bool) -> str | None:
    """``"S"`` / ``"B"`` for a route method the automatic guard covers, else
    ``None`` (spec §4.7 assignment table)."""
    if explicit_guard or policy.auth != "user" or not policy.is_write:
        return None
    if policy.no_idempotency is not None or policy.operation_intent is not None:
        return None
    if policy.replay_safe is not None and not policy.risk:
        return "B"
    return "S"


def _scope_name(method: str, path: str) -> str:
    return f"auto:{method} {path}"


async def _guard(request: Any, scope: Any, mode: str) -> None:
    key = await _request_key(request, required=False)
    if key is None:
        return
    if scope.risk is not None and scope.risk.step_up:
        _risk.settle_step_up(scope)  # a replay is decided after the step-up
    name = _scope_name(request.method, scope.policy.path)
    units = _request.active_units()
    write_store = scope.policy.writes
    session = None
    for unit in reversed(units):
        if write_store is None or unit.info.get(_DB_NAME_KEY) == write_store:
            session = unit
            break
    if session is None and scope.lazy_factory is not None and scope.intent is None:
        # An ordinary write that will use db(): open the lazy request unit
        # NOW (keyed writes only), so the durable record (mode S/B) is claimed
        # in the unit the handler's writes commit in — never the in-flight
        # form, which a sequential retry would run again (G-IDEM-17). Reads
        # and unkeyed writes keep the lazy unit unopened (0 connections).
        session = _request._lazy_unit(scope)
    store = None
    if session is not None:
        db_name = session.info.get(_DB_NAME_KEY) or "default"
        store = request.app.state.core.database(db_name).request_log()
    if session is None or store is None:
        # no request unit (or no durable log on it): the fail-closed in-flight
        # form (G-IDEM-18)
        guard = _cache_guard(
            scope=name,
            ttl_seconds=_DEFAULT_TTL_SECONDS,
            required=False,
            fail_closed=True,
            store_response=False,
        )
        await guard(request)
        return
    await claim_durable(
        request, session, store, name, key, _TTL, status_only=mode != "B"
    )


def _mark_coroutine_function(fn: Any) -> None:
    """Make FastAPI await ``fn`` (a plain function returning an awaitable):
    ``inspect.markcoroutinefunction`` (3.12+; FastAPI's test on 3.13+) and
    the ``asyncio`` marker (its test before 3.13)."""
    mark = getattr(inspect, "markcoroutinefunction", None)
    if mark is not None:
        mark(fn)
    marker = getattr(asyncio.coroutines, "_is_coroutine", None)
    if marker is not None:
        fn._is_coroutine = marker


def install_automatic_guard(route: Any, modes: dict[str, str]) -> bool:
    """Wrap ``route``'s endpoint call for the methods in ``modes`` (before the
    route handler is built). Generator / streaming endpoints are left alone."""
    if not modes or route.__dict__.get(AUTO_ATTR):
        return False
    dependant = route.dependant
    original: Callable[..., Any] = dependant.call
    unwrapped = inspect.unwrap(original)
    if inspect.isasyncgenfunction(unwrapped) or inspect.isgeneratorfunction(unwrapped):
        return False
    is_async = inspect.iscoroutinefunction(unwrapped) or inspect.iscoroutinefunction(
        original
    )

    async def guarded(request: Any, scope: Any, mode: str, values: Any) -> Any:
        await _guard(request, scope, mode)
        if is_async:
            return await original(**values)
        return await run_in_threadpool(original, **values)

    async def plain_sync(values: Any) -> Any:
        return await run_in_threadpool(original, **values)

    @functools.wraps(original)
    def endpoint(**values: Any) -> Any:
        # A plain function returning the awaitable FastAPI awaits: a request
        # without Idempotency-Key gets the endpoint's own coroutine (no extra
        # coroutine layer on every write).
        scope = _request.request_scope_get()
        if scope is not None:
            request = scope.request
            mode = modes.get(getattr(request, "method", ""))
            if mode is not None and request.headers.get("idempotency-key"):
                return guarded(request, scope, mode, values)
        if is_async:
            return original(**values)
        return plain_sync(values)

    _mark_coroutine_function(endpoint)
    dependant.call = endpoint
    dependant.__dict__.pop("is_coroutine_callable", None)
    route.__dict__[AUTO_ATTR] = dict(modes)
    return True


__all__ = ["AUTO_ATTR", "automatic_mode", "install_automatic_guard"]
