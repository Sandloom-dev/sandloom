"""The durable idempotency guard (``idempotency_guard(durable=True)``).

ADR-0005's HTTP adapter of the durable dedupe record: the key is claimed in
the REQUEST's own Unit of Work (``http_idempotency_log``) and completed with
the response right before ``COMMIT``, so the record commits iff the effect
commits. Also the startup checks that read a route's guard mode
(:func:`install_durable_capture`, :func:`validate_money_guards`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

import orjson
from fastapi import Depends, HTTPException, Request, Response
from fastapi.routing import request_response

import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import effects
from sandloom.core.guard_order import (
    iter_api_routes,
    mark_idempotency_guard,
    resolution_order,
)
from sandloom.core.idempotency.fingerprint import (
    _caller_identity,
    _parse_record,
    _request_fingerprint,
    _request_key,
    _tenant_identity,
)
from sandloom.core.idempotency.replay import (
    _JSON_BODY,
    _ReplayResponse,
    stored_response,
)
from sandloom.core.infra.contracts import RequestLogBusy
from sandloom.core.route_policy import CapabilityReplayError, resolve_route_policy
from sandloom.core.unit_of_work.hooks import queue_before_commit

logger = logging.getLogger("sandloom.core.idempotency")


# --------------------------------------------------------------------------- #
# Durable form (ADR-0005: the HTTP adapter of the durable dedupe record)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GuardMode:
    """How an ``idempotency_guard`` dependency dedupes (read by startup checks).

    ``kind`` is ``"durable"`` (record in the request's transaction),
    ``"cache"`` (replay-only cache record) or ``"in_flight"``
    (``store_response=False``: in-flight dedupe in front of the endpoint's own
    durable record).
    """

    kind: Literal["durable", "cache", "in_flight"]
    fail_closed: bool


#: ``(dependency, FastAPI scope)`` of the request's default-database Unit of
#: Work session (``DbSession``), bound by :mod:`sandloom.core.di` at import: the
#: durable guard must share the endpoint's session (same callable -> FastAPI's
#: per-request dependency cache gives both the same UoW).
_REQUEST_SESSION: tuple[Callable[..., Any], Literal["function"]] | None = None


def bind_request_session(
    dependency: Callable[..., Any], *, scope: Literal["function"]
) -> None:
    """Register the request session dependency (``sandloom.core.di`` only)."""
    global _REQUEST_SESSION
    _REQUEST_SESSION = (dependency, scope)


#: Attribute of a guard dependency holding its :class:`GuardMode`.
_MODE_ATTR = "__sandloom_idempotency_mode__"
#: ``request.state`` attribute of a request a durable guard claimed for.
_DURABLE_ATTR = "_sandloom_durable_idempotency"
_NOT_REPLAYABLE_DETAIL = (
    "Idempotency-Key already used; the original response cannot be replayed"
)


def guard_mode(call: Any) -> GuardMode | None:
    """The :class:`GuardMode` of an idempotency guard dependency, else ``None``."""
    mode = getattr(call, _MODE_ATTR, None)
    return mode if isinstance(mode, GuardMode) else None


class _DurableClaim:
    """A durable claim of one request: completed right before its COMMIT."""

    __slots__ = ("store", "session", "dedupe_key", "anonymous", "response")

    def __init__(
        self, store: Any, session: Any, dedupe_key: str, anonymous: bool
    ) -> None:
        self.store = store
        self.session = session
        self.dedupe_key = dedupe_key
        self.anonymous = anonymous
        #: The endpoint's response, set by the route's capture wrapper
        #: (:func:`install_durable_capture`) before the UoW commits.
        self.response: Response | None = None

    async def complete(self) -> None:
        """Before-commit stage: store the response on the claimed record."""
        response = self.response
        if response is None:
            # No response captured (a route added after startup, outside the
            # capture wrapper): the effect commits; a retry is refused (409),
            # never re-run.
            await self.store.complete(
                self.session,
                dedupe_key=self.dedupe_key,
                status=effects.NOT_REPLAYABLE,
                result=None,
            )
            return
        status_code = int(response.status_code)
        if status_code >= 500:
            # A 5xx is never replayed: drop the claim (a retry runs afresh).
            await self.store.release(self.session, dedupe_key=self.dedupe_key)
            return
        body = getattr(response, "body", None)  # a streaming response has none
        headers = {
            k: v for k, v in response.headers.items() if k.lower() != "set-cookie"
        }
        ctype = response.headers.get("content-type")
        media_type = ctype.split(";", 1)[0].strip() if ctype else None
        replayable = (
            isinstance(body, bytes)
            and effects.fits_replay(len(body))
            and not self.anonymous
        )
        stored = stored_response(
            status_code=status_code,
            body=body if replayable and isinstance(body, bytes) else b"",
            headers=headers,
            media_type=media_type,
            replayable=replayable,
        )
        await self.store.complete(
            self.session,
            dedupe_key=self.dedupe_key,
            status=effects.SUCCEEDED if replayable else effects.NOT_REPLAYABLE,
            result=orjson.dumps(stored).decode(),
        )


def _durable_replay(status: str | None, result: str | None) -> Exception:
    """The replay of a committed durable record (a stored response), or a
    ``409`` when nothing replayable was kept."""
    stored = None if result is None else _parse_record(result)
    if stored is None or stored.get("state") == "corrupt" or "status" not in stored:
        return HTTPException(status_code=409, detail=_NOT_REPLAYABLE_DETAIL)
    if status != effects.SUCCEEDED:
        stored["truncated"] = True
        stored.pop(_JSON_BODY, None)
    return _ReplayResponse(stored)


async def claim_durable(
    request: Request,
    db: Any,
    store: Any,
    scope: str,
    key: str,
    ttl: int,
    *,
    status_only: bool = False,
) -> _DurableClaim:
    """Claim ``key`` in ``db``'s request log (the durable record of ADR-0005):
    ``409`` while in flight, ``422`` on a different request, the stored
    response (or its status, ``Idempotent-Replay``) after success. A fresh
    claim is completed with the response right before ``db``'s COMMIT.
    ``status_only`` stores and replays the status alone (mode S, spec §4.7)."""
    caller = _caller_identity(request)
    tenant = _tenant_identity(request)
    fingerprint = await _request_fingerprint(request, caller, tenant)
    dedupe_key = effects.key_digest("http", scope, caller, tenant, key)
    try:
        claim = await store.claim(
            db,
            dedupe_key=dedupe_key,
            scope=scope,
            tenant=None if tenant == "t:none" else tenant[2:66],
            fingerprint=fingerprint,
            ttl_seconds=ttl,
        )
    except RequestLogBusy:
        raise HTTPException(
            status_code=409,
            detail="A request with this Idempotency-Key is still in progress",
        ) from None
    if claim.outcome == "mismatch":
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key reused with a different request",
        )
    if claim.outcome == "replay":
        raise _durable_replay(claim.status, claim.result)
    pending = _DurableClaim(
        store, db, dedupe_key, status_only or caller.startswith("anon:")
    )
    setattr(request.state, _DURABLE_ATTR, pending)
    queue_before_commit(db, pending.complete)
    _risk.bind_operation_key(dedupe_key)
    return pending


def _durable_guard(
    *, scope: str, ttl_seconds: int, required: bool
) -> Callable[..., Awaitable[str | None]]:

    if (
        _REQUEST_SESSION is None
    ):  # pragma: no cover - sandloom.core.di binds it at import
        raise RuntimeError(
            "idempotency_guard(durable=True) needs the request session "
            "dependency: import sandloom.core.di first"
        )
    session_dependency, session_scope = _REQUEST_SESSION
    ttl = effects.ttl_or_default(ttl_seconds)

    async def _dependency(
        request: Request,
        db: Any = Depends(session_dependency, scope=session_scope),
    ) -> str | None:
        key = await _request_key(request, required)
        if key is None:
            return None
        store = request.app.state.core.database("default").request_log()
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Durable idempotency is unavailable on this database",
            )
        await claim_durable(request, db, store, scope, key, ttl)
        route = request.scope.get("route")
        if route is not None and not route.__dict__.get(_CAPTURING_ATTR):
            # A route outside the route index's reach (an app without it,
            # added after startup): wrap it now for the NEXT request; this
            # one completes without a response (a retry gets 409).
            logger.warning(
                "durable idempotency_guard on %s ran without response capture; "
                "installing it (call install_durable_capture after adding routes)",
                getattr(route, "path", "?"),
            )
            install_durable_capture([route])
        return key

    setattr(_dependency, _MODE_ATTR, GuardMode("durable", True))
    return mark_idempotency_guard(_dependency)


def _capturing(
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    async def _handler(request: Request) -> Response:
        response = await handler(request)
        pending = request.scope.get("state", {}).get(_DURABLE_ATTR)
        if pending is not None:
            pending.response = response
        return response

    return _handler


#: Route ``__dict__`` key marking a route whose handler captures responses.
_CAPTURING_ATTR = "_sandloom_durable_capture"


def _guard_modes(route: Any) -> Iterator[GuardMode]:
    for call in resolution_order(route.dependant):
        mode = guard_mode(call)
        if mode is not None:
            yield mode


def install_durable_capture(routes: Iterable[Any]) -> int:
    """Let every route with a durable guard hand its response to the claim.

    FastAPI builds the response INSIDE the function-scoped dependency stack,
    whose exit commits the request's Unit of Work — so a handler wrapper sees
    the finished response (status, headers, body) before ``COMMIT``. Called
    by ``create_app`` once every router is included; rebuilds only those
    routes' ``app`` (``request_response`` over the route's own handler).
    Returns the number of routes wrapped.
    """
    wrapped = 0
    for route in iter_api_routes(routes):
        if route.__dict__.get(_CAPTURING_ATTR) or not any(
            mode.kind == "durable" for mode in _guard_modes(route)
        ):
            continue
        route.app = request_response(_capturing(route.get_route_handler()))
        route.__dict__[_CAPTURING_ATTR] = True
        wrapped += 1
    return wrapped


def money_guard_problems(routes: Iterable[Any]) -> list[str]:
    """Every money route (``sensitive("money")``) whose idempotency guard could
    re-run its effect or replay a body (SANDLOOM-B019; replaces the class-F check).

    Accepted: ``durable=True``, or ``store_response=False`` with
    ``fail_closed=True`` (in-flight dedupe in front of a dedupe record the
    endpoint owns — a ledger operation key). Refused: a cache guard that
    stores responses (replay-only: an ambiguous COMMIT or a crash before its
    terminal write re-runs the effect, and a sensitive route never replays a
    body, spec §4.7) and any fail-open guard. A money route WITHOUT a guard is
    not reported here: its money effect needs its own dedupe record or the
    client's key at run time (SANDLOOM-R009 / SANDLOOM-R010).
    """
    problems: list[str] = []
    for route in iter_api_routes(routes):
        if "money" not in resolve_route_policy(route).risk:
            continue
        for mode in _guard_modes(route):
            in_flight_ok = mode.kind == "in_flight" and mode.fail_closed
            if mode.kind == "durable" or in_flight_ok:
                continue
            kind = "cache (replay-only)" if mode.kind == "cache" else "in-flight"
            opening = "" if mode.fail_closed else "fail-open "
            methods = ",".join(sorted(route.methods or ())) or "?"
            problems.append(
                f"{methods} {route.path}: a money route uses a "
                f"{opening}{kind} idempotency_guard — use "
                "idempotency_guard(..., durable=True) (or store_response=False "
                "+ fail_closed=True when the endpoint owns its durable dedupe "
                "record)"
            )
    return problems


def validate_money_guards(routes: Iterable[Any]) -> None:
    """Startup check: raise :class:`CapabilityReplayError` (SANDLOOM-B019) listing
    every problem of :func:`money_guard_problems`."""
    problems = money_guard_problems(routes)
    if problems:
        raise CapabilityReplayError(
            "; ".join(problems) + " (sandloom explain SANDLOOM-B019)"
        )
