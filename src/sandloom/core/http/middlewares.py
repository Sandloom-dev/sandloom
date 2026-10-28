"""Framework HTTP middlewares: request-id, security headers, CORS, timing.

Installed by the app factory via register_middlewares(app). Kept separate from
error handling (sandloom.core.http.error_handlers) so each concern stays small.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, cast

from fastapi import FastAPI, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware

import sandloom.core.http.request_authority as _request_authority
import sandloom.core.idempotency.capture as _idempotency_capture
import sandloom.core.observability.metrics as _metrics
import sandloom.core.observability.request_context as _request_context
from sandloom.core.config.settings import settings
from sandloom.core.http.error_handlers import (
    Mask5xxMiddleware,
    RouteErrorMiddleware,
    _BodyTooLarge,
)
from sandloom.core.ingress.rate_limit import GlobalRateLimitMiddleware

_logger = logging.getLogger("sandloom.core.http.middlewares")

# Current request id, available to logging/tracing without threading it around.
# Defined in the foundation layer (observability.request_context) and bound by
# assignment, not ``from ... import``: CPython compiles ``name.set()`` without
# the method-call fast path when ``name`` came from an import statement.
request_id_var: ContextVar[str | None] = _request_context.request_id_var

_REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LEN = 128
_REQUEST_ID_HEADER_BYTES = b"x-request-id"
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
)
_HSTS_HEADER = (
    b"strict-transport-security",
    b"max-age=63072000; includeSubDomains",
)


def _pipeline_request_id(scope: Any) -> str:
    for key, value in scope.get("headers", ()):
        if key == _REQUEST_ID_HEADER_BYTES:
            incoming: str = value.decode("latin-1")
            if incoming and incoming.isprintable():
                return incoming[:_MAX_REQUEST_ID_LEN]
            break
    return str(uuid.uuid4())


def _add_security_headers(raw: list[tuple[bytes, bytes]]) -> None:
    present = {key.lower() for key, _ in raw}
    raw.extend(header for header in _SECURITY_HEADERS if header[0] not in present)
    if settings.is_prod and _HSTS_HEADER[0] not in present:
        raw.append(_HSTS_HEADER)
    media_type = next(
        (
            value.split(b";", 1)[0].strip().lower()
            for key, value in raw
            if key.lower() == b"content-type"
        ),
        b"",
    )
    # JSON API documents need no active content. Leave HTML applications and
    # Swagger to their own CSP so their assets and login flows stay usable.
    if media_type == b"application/json" or media_type.endswith(b"+json"):
        raw.extend(header for header in _JSON_HEADERS if header[0] not in present)


class RequestIDMiddleware:
    """Propagate (or mint) a request id and echo it on the response.

    Pure ASGI (not ``BaseHTTPMiddleware``) so it runs inline in the request task
    rather than spawning an anyio task per request.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(_REQUEST_ID_HEADER, "")
        request_id = (
            incoming[:_MAX_REQUEST_ID_LEN]
            if incoming and incoming.isprintable()
            else str(uuid.uuid4())
        )

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(raw=message["headers"])[_REQUEST_ID_HEADER] = request_id
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Baseline security headers for an API service (pure ASGI)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                _add_security_headers(message["headers"])
            await send(message)

        await self.app(scope, receive, send_wrapper)


_KNOWN_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)


def _method_label(request: Request) -> str:
    """Clamp the request method to a known verb for metric labels.

    ``request.method`` is client-supplied; an attacker sending arbitrary verbs
    would otherwise mint an unbounded Prometheus child series per distinct
    method. Unknown verbs collapse to ``OTHER``.
    """
    method = request.method.upper()
    return method if method in _KNOWN_METHODS else "OTHER"


def _route_template(request: Request) -> str:
    """Return the matched route template (low cardinality) or a safe fallback.

    Using the template (``/users/{id}``) instead of the raw path keeps the
    metric label cardinality bounded; unmatched paths collapse to ``<unmatched>``.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "<unmatched>"


#: Upper bound on cached labelled metric children per middleware instance. Route
#: templates are finite (unmatched paths collapse to ``<unmatched>``) and methods
#: are clamped, so this is never reached by a sane app; past it the middleware
#: falls back to ``.labels()`` per request (same series, just slower).
_METRIC_CHILD_CACHE_MAX = 4096
_PROCESS_TIME_HEADER = b"x-process-time"


def _set_raw_header(raw: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    """``MutableHeaders(raw=raw)[name] = value`` without building the wrapper:
    replace the first occurrence (dropping duplicates) or append."""
    found = -1
    i = 0
    while i < len(raw):
        if raw[i][0].lower() == name:
            if found < 0:
                found = i
                raw[i] = (name, value)
            else:
                del raw[i]
                continue
        i += 1
    if found < 0:
        raw.append((name, value))


class ProcessTimeMiddleware:
    """Emit the X-Process-Time header and HTTP RED metrics per request.

    Pure ASGI: the status is read off ``http.response.start`` and the timing
    header injected there, avoiding the per-request task spawned by
    ``BaseHTTPMiddleware``. The labelled Prometheus children are cached per
    ``(method, route template, status)`` — ``.labels()`` returns the very same
    child object every time, so the exposition is identical — and the method /
    route template are read straight off the ASGI scope (no ``Request``).
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._metrics: Any = None
        self._in_progress: dict[str, Any] = {}
        self._done: dict[tuple[str, str, int], tuple[Any, Any]] = {}

    def _bind_metrics(self) -> Any:
        metrics = _metrics

        # Re-bind (and drop cached children) if the metric objects were swapped.
        key = (
            metrics.http_requests_in_progress,
            metrics.http_request_duration_seconds,
            metrics.http_requests_total,
        )
        if self._metrics != key:
            self._metrics = key
            self._in_progress.clear()
            self._done.clear()
        return metrics

    def _gauge(self, method: str) -> Any:
        child = self._in_progress.get(method)
        if child is None:
            child = self._metrics[0].labels(method=method)
            self._in_progress[method] = child
        return child

    def _finished(self, method: str, path: str, status: int) -> tuple[Any, Any]:
        key = (method, path, status)
        pair = self._done.get(key)
        if pair is None:
            pair = (
                self._metrics[1].labels(method=method, path=path),
                self._metrics[2].labels(method=method, path=path, status=str(status)),
            )
            if len(self._done) < _METRIC_CHILD_CACHE_MAX:
                self._done[key] = pair
        return pair

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        self._bind_metrics()
        method = scope["method"].upper()
        if method not in _KNOWN_METHODS:
            method = "OTHER"
        start_time = time.perf_counter()
        status = 500
        gauge = self._gauge(method)
        gauge.inc()

        async def send_wrapper(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                _set_raw_header(
                    message["headers"],
                    _PROCESS_TIME_HEADER,
                    str(time.perf_counter() - start_time).encode("latin-1"),
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start_time
            route = scope.get("route")
            path = getattr(route, "path", None)
            if not isinstance(path, str) or not path:
                path = "<unmatched>"
            gauge.dec()
            duration, total = self._finished(method, path, status)
            duration.observe(elapsed)
            total.inc()


def _finish_response_headers(  # noqa: C901
    raw: list[tuple[bytes, bytes]], process_time: bytes, request_id: bytes
) -> None:
    """The pipeline's response headers in ONE pass over ``raw``.

    Exactly ``_set_raw_header(raw, x-process-time)``, then
    :func:`_add_security_headers`, then ``_set_raw_header(raw, x-request-id)``
    (same resulting list, same order), for a response no idempotency capture
    has to see between the timing header and the others.
    """
    out: list[tuple[bytes, bytes]] = []
    present: set[bytes] = set()
    media_type: bytes | None = None
    timed = identified = False
    for key, value in raw:
        lowered = key.lower()
        if lowered == _PROCESS_TIME_HEADER:
            if not timed:
                timed = True
                out.append((_PROCESS_TIME_HEADER, process_time))
            continue
        if lowered == _REQUEST_ID_HEADER_BYTES:
            present.add(lowered)
            if not identified:
                identified = True
                out.append((_REQUEST_ID_HEADER_BYTES, request_id))
            continue
        present.add(lowered)
        if media_type is None and lowered == b"content-type":
            media_type = value.split(b";", 1)[0].strip().lower()
        out.append((key, value))
    if not timed:
        out.append((_PROCESS_TIME_HEADER, process_time))
    out.extend(header for header in _SECURITY_HEADERS if header[0] not in present)
    if settings.is_prod and _HSTS_HEADER[0] not in present:
        out.append(_HSTS_HEADER)
    if media_type is not None and (
        media_type == b"application/json" or media_type.endswith(b"+json")
    ):
        out.extend(header for header in _JSON_HEADERS if header[0] not in present)
    if not identified:
        out.append((_REQUEST_ID_HEADER_BYTES, request_id))
    raw[:] = out


class SandloomPipelineMiddleware(ProcessTimeMiddleware):
    """Request context, response headers, metrics and idempotency in one layer.

    The response capture keeps its existing state machine and ordering: timing
    is added before capture, security/request headers afterwards. Error masks,
    CORS and the streaming body limit retain their separate boundaries. The
    standalone middleware classes remain available for custom compositions.

    The capture exists only for a response whose request an
    ``idempotency_guard`` armed (read when the response starts, as the capture
    itself does); every other response takes one header pass and no capture
    awaits. An armed request that never started a response is released at
    the end exactly as the capture's idle state does.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: C901
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = _pipeline_request_id(scope)
        token = request_id_var.set(request_id)
        try:
            state = scope.setdefault("state", {})
            capture: Any = None
            started = False
            self._bind_metrics()
            method = scope["method"].upper()
            if method not in _KNOWN_METHODS:
                method = "OTHER"
            start_time = time.perf_counter()
            status = 500
            gauge = self._gauge(method)
            gauge.inc()

            async def send_wrapper(message: Any) -> None:
                nonlocal status, capture, started
                if message["type"] == "http.response.start":
                    status = message["status"]
                    elapsed = str(time.perf_counter() - start_time).encode("latin-1")
                    if not started:
                        started = True
                        if state.get(_IDEMPOTENCY_ARMED_ATTR) is not None:
                            capture = _idempotency_capture._ResponseCapture(state)
                    if capture is None:
                        _finish_response_headers(
                            message["headers"],
                            elapsed,
                            request_id.encode("latin-1"),
                        )
                        await send(message)
                        return
                    _set_raw_header(message["headers"], _PROCESS_TIME_HEADER, elapsed)
                    await capture.before_send(message)
                    _add_security_headers(message["headers"])
                    _set_raw_header(
                        message["headers"],
                        _REQUEST_ID_HEADER_BYTES,
                        request_id.encode("latin-1"),
                    )
                    await send(message)
                    await capture.after_send(message)
                    return
                if capture is None:
                    await send(message)
                    return
                await capture.before_send(message)
                await send(message)
                await capture.after_send(message)

            try:
                try:
                    await self.app(scope, receive, send_wrapper)
                finally:
                    elapsed = time.perf_counter() - start_time
                    route = scope.get("route")
                    path = getattr(route, "path", None)
                    if not isinstance(path, str) or not path:
                        path = "<unmatched>"
                    gauge.dec()
                    duration, total = self._finished(method, path, status)
                    duration.observe(elapsed)
                    total.inc()
            except BaseException:
                await self._settle(capture, started, state)
                raise
            await self._settle(capture, started, state)
        finally:
            request_id_var.reset(token)

    @staticmethod
    async def _settle(capture: Any, started: bool, state: dict[str, Any]) -> None:
        if capture is not None:
            await capture.on_abort()
        elif not started and state.get(_IDEMPOTENCY_ARMED_ATTR) is not None:
            # No response went out and the guard armed the key: the endpoint
            # (or a dependency) raised — release it (the capture's idle state).
            await _idempotency_capture._ResponseCapture(state).on_abort()


#: ``request.state`` key an ``idempotency_guard`` arms (``sandloom.core.idempotency``).
_IDEMPOTENCY_ARMED_ATTR = "_sandloom_idempotency"
_JSON_HEADERS = (
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds the configured cap.

    Guards JSON/form endpoints against large-body DoS. The cap defaults to the
    upload size limit so legitimate multipart uploads still pass.

    Two layers, because trusting ``Content-Length`` alone is not a cap — a
    ``Transfer-Encoding: chunked`` or header-less body would otherwise stream
    through unbounded (the exact body-DoS path):

    1. fast path — reject immediately when a declared ``Content-Length`` already
       exceeds the limit (no bytes read);
    2. streaming guard — a running byte counter wraps the ASGI ``receive`` so
       the request is aborted with ``413`` the moment accumulated body bytes
       cross the limit, regardless of the declared length.

    Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so it can wrap
    ``receive`` and short-circuit ``send``.
    """

    def __init__(self, app: Any, *, max_bytes: int | None = None) -> None:
        self.app = app
        self._max_bytes = max_bytes

    @classmethod
    async def _send_413(cls, send: Any) -> None:
        await cls._send_error(
            send, 413, "Request body too large", "request_entity_too_large"
        )

    @staticmethod
    async def _send_error(send: Any, status: int, detail: str, code: str) -> None:
        body = json.dumps({"detail": detail, "code": code}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: C901
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = (
            settings.effective_max_request_body_bytes
            if self._max_bytes is None
            else self._max_bytes
        )
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        # Fast path: a declared Content-Length over the cap is rejected before
        # any body is read. Every Content-Length header counts: conflicting
        # values are a request-smuggling shape (RFC 9112 6.3: unrecoverable)
        # and are refused; identical duplicates act as one. Unparsable values
        # are left to the streaming guard below, which caps the bytes anyway.
        declared = {
            value
            for name, value in scope.get("headers") or []
            if name == b"content-length"
        }
        if len(declared) > 1:
            await self._send_error(
                send, 400, "Conflicting Content-Length", "bad_request"
            )
            return
        if declared:
            try:
                too_large = int(next(iter(declared))) > limit
            except ValueError:
                too_large = False
            if too_large:
                await self._send_413(send)
                return

        received = 0

        async def counting_receive() -> Any:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > limit:
                    raise _BodyTooLarge()
            return message

        response_started = False

        async def guarded_send(message: Any) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            # The handler hadn't started its response yet (it was still reading
            # the oversized body) — safe to emit our own 413.
            if not response_started:
                await self._send_413(send)
            else:  # pragma: no cover - response already in flight
                raise


def register_middlewares(app: FastAPI) -> None:
    """Install framework middlewares (order: added last runs first)."""
    app.user_middleware = [
        m
        for m in app.user_middleware
        if cast(Any, m.cls) not in (Mask5xxMiddleware, RouteErrorMiddleware)
    ]
    # Innermost: endpoint exceptions become responses every middleware sees.
    app.add_middleware(RouteErrorMiddleware)
    # The body cap sits directly around the app: its streaming counter must be
    # the receive() the endpoint reads through, so the 413 signal surfaces in
    # FastAPI's body parsing (an HTTPException -> 413). Placed outside a
    # task-group-based middleware it would arrive wrapped in an ExceptionGroup
    # and FastAPI would turn it into a generic 400.
    app.add_middleware(BodySizeLimitMiddleware)
    if settings.rate_limit_global_requests > 0:
        # Opt-in per-IP floor: inside the pipeline (request id, RED metrics and
        # security headers cover its 429), outside the body cap (no body read).
        app.add_middleware(
            GlobalRateLimitMiddleware,
            requests=settings.rate_limit_global_requests,
            period=settings.rate_limit_global_period_seconds,
            exempt=tuple(settings.rate_limit_global_exempt_path_prefixes),
        )
    app.add_middleware(SandloomPipelineMiddleware)
    # 5xx safety net INSIDE CORS: a masked 500 must still carry the CORS
    # headers or the browser reports an opaque CORS error instead. If
    # register_error_handlers already installed it (called first), it was
    # removed above and is re-added here at the right position.
    app.add_middleware(Mask5xxMiddleware)
    origins = settings.cors_origins or [settings.frontend_url]
    # A wildcard origin combined with credentials is a CORS footgun: Starlette
    # would reflect the request Origin back with ``Allow-Credentials: true``,
    # letting any site make authenticated cross-origin requests. If an operator
    # configures ``*``, disable credentials rather than silently allow-all.
    allow_credentials = "*" not in origins
    if not allow_credentials:
        _logger.warning(
            "CORS_ORIGINS contains '*'; disabling allow_credentials to avoid "
            "reflecting any origin with credentials. Set explicit origins to "
            "allow credentialed cross-origin requests."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        # X-Total-Count: list totals (identity's user list) for browser UIs.
        expose_headers=[_REQUEST_ID_HEADER, "X-Total-Count"],
    )

    _request_authority.install_request_authority(app)
