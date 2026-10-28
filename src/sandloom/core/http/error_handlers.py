"""Framework-wide error handling: standard error format + 5xx masking.

Single source of truth for app error handlers. Installed by the app factory
via register_error_handlers(app). Response contract: {"detail": ..., "code": ...}.

HTTP-status exception protocol
------------------------------
The kernel must not import policy types from ``sandloom.shared`` or business
modules, so domain exceptions are mapped to HTTP by *protocol*, not by class:
any exception exposing an integer ``http_status`` attribute (400-599) is
rendered by the framework as that status with body
``{"detail": <exc.detail or str(exc)>, "code": <exc.code or "error">}`` and
optional ``exc.headers`` (a ``dict[str, str]``). Example::
    class AccessDenied(Exception):
        http_status = 403
        code = "access_denied"
        detail = "Forbidden"

Everything else that escapes an endpoint is logged (type + sanitized message,
never bound SQL parameters) and masked as a generic 500. The response stays
opaque in every profile; an in-process observer (:func:`observe_unhandled`,
used by the test client of :mod:`sandloom.core.testing`) receives the original
exception, so a test sees the cause instead of ``internal_error``.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

import sandloom.core.idempotency.replay as _replay
from sandloom.core.exceptions import AppException
from sandloom.core.observability.guardrails import scrub_text
from sandloom.core.tenant_context import MissingTenantError

logger = logging.getLogger("sandloom.core.errors")

_INTERNAL_ERROR_BODY = {"detail": "Internal server error", "code": "internal_error"}

#: ``(method, path, exception)`` callbacks told about every masked 500.
UnhandledObserver = Callable[[str, str, BaseException], None]
_unhandled_observers: list[UnhandledObserver] = []


@contextmanager
def observe_unhandled(observer: UnhandledObserver) -> Iterator[None]:
    """Call ``observer(method, path, exc)`` for every exception masked as 500.

    In-process only (the response is unchanged): the test harness uses it to
    re-raise the original exception at the assertion instead of a bare
    ``internal_error`` (SANDLOOM-R006).
    """
    _unhandled_observers.append(observer)
    try:
        yield
    finally:
        _unhandled_observers.remove(observer)


def _report_unhandled(scope: Any, exc: BaseException) -> None:
    if not _unhandled_observers:
        return
    method = str(scope.get("method", "?"))
    path = str(scope.get("path", "?"))
    for observer in _unhandled_observers:
        observer(method, path, exc)


def describe_exception(exc: BaseException) -> str:
    """``"<Type>: <sanitized message>"`` safe to log.

    SQLAlchemy-style statement errors (anything carrying ``statement`` +
    ``params``) are reduced to their type and the underlying driver error type —
    their ``str()`` embeds the bound parameters (emails, tokens, password
    hashes). Other messages are scrubbed of DSN credentials, ``[parameters:
    ...]`` blocks and ``secret=value`` pairs, and truncated.
    """
    name = type(exc).__name__
    if hasattr(exc, "statement") and hasattr(exc, "params"):
        orig = getattr(exc, "orig", None)
        inner = f" ({type(orig).__name__})" if orig is not None else ""
        return f"{name}{inner}: database statement failed (parameters redacted)"
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 - a broken __str__ must not break logging
        message = "<unprintable>"
    message = scrub_text(message)
    if len(message) > 500:
        message = message[:500] + "…"
    return f"{name}: {message}" if message else name


def http_status_of(exc: BaseException) -> int | None:
    """The status an exception declares via the ``http_status`` protocol."""
    raw = getattr(exc, "http_status", None)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 400 <= raw <= 599 else None


def protocol_response(exc: BaseException, status_code: int) -> JSONResponse:
    """Render an ``http_status``-protocol exception."""
    detail = getattr(exc, "detail", None)
    if detail is None:
        detail = str(exc) or None
    code = getattr(exc, "code", None) or "error"
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=status_code,
        content={"detail": jsonable_encoder(detail), "code": code},
        headers=headers if isinstance(headers, dict) else None,
    )


def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle HTTPException and return a JSON response.

    Note: Starlette expects handlers typed as (Request, Exception) -> Response.
    We cast to HTTPException to access specific attributes. Headers set on the
    exception (e.g. WWW-Authenticate, X-2FA-Required) are preserved.
    """
    http_exc = cast(HTTPException, exc)
    return JSONResponse(
        status_code=http_exc.status_code,
        content={"detail": http_exc.detail, "code": getattr(http_exc, "code", None)},
        headers=http_exc.headers,
    )


def missing_tenant_handler(request: Request, exc: Exception) -> Response:
    """A ``MissingTenantError`` that reaches the request boundary: the same
    ``400 tenant context required`` ``TenantScope()`` answers (G-TENANT-12),
    never a bare 500. The message (it names models / operations) stays in the
    log."""
    logger.info("tenant context required: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "tenant context required", "code": None},
    )


def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle validation errors raised by FastAPI/Starlette.

    Returns only the fields a client needs to fix the request — ``type``,
    ``loc`` and ``msg``. Pydantic v2 also attaches ``input`` (the submitted
    value, echoing back potentially sensitive data) and ``ctx`` (validator
    internals, sometimes carrying the non-serializable original exception); both
    are stripped so a public 422 never reflects request data or internal shape.
    """
    val_exc = cast(RequestValidationError, exc)
    safe_errors = [
        {key: value for key, value in err.items() if key in {"type", "loc", "msg"}}
        for err in val_exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": jsonable_encoder(safe_errors, custom_encoder={ValueError: str}),
            "code": "validation_error",
        },
    )


def app_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle domain AppException subclasses (NotFound, AuthError, ...)."""
    app_exc = cast(AppException, exc)
    return JSONResponse(
        status_code=getattr(app_exc, "status_code", 400),
        content={
            "detail": str(app_exc),
            "code": getattr(app_exc, "error_code", "app_error"),
        },
    )


def generic_exception_handler(request: Request, exc: Exception) -> Response:
    # Starlette hands this handler to ServerErrorMiddleware too, which also
    # sees HTTPExceptions raised by middlewares outside the framework's 5xx
    # mask (never rendered by ExceptionMiddleware): keep their status.
    if isinstance(exc, StarletteHTTPException):
        return http_exception_handler(request, exc)
    declared = http_status_of(exc)
    if declared is not None:
        return protocol_response(exc, declared)
    logger.error("Unhandled error: %s", describe_exception(exc), exc_info=True)
    _report_unhandled(getattr(request, "scope", None) or {}, exc)
    return JSONResponse(status_code=500, content=dict(_INTERNAL_ERROR_BODY))


class _BodyTooLarge(HTTPException):
    """Signal: the request body exceeded the configured cap.

    An ``HTTPException(413)`` so that when FastAPI is mid-way through parsing a
    JSON/form body (it wraps arbitrary read errors into a generic 400 but
    re-raises ``HTTPException`` untouched) the client still gets a 413 rendered
    by the framework handler, not "There was an error parsing the body".
    """

    code = "request_entity_too_large"

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


class Mask5xxMiddleware:
    """Safety net: map ``http_status``-protocol exceptions, mask the rest as 500.

    Pure ASGI (no ``BaseHTTPMiddleware`` task per request).
    :func:`sandloom.core.http.middlewares.register_middlewares` installs it TWICE:

    * innermost (as :class:`RouteErrorMiddleware`, right around the router) so
      an exception escaping an endpoint becomes a proper response that every
      framework middleware sees — metrics record the real status, request-id /
      security / CORS headers are added, idempotency observes the outcome;
    * just inside CORS, to also mask exceptions raised by the other framework
      middlewares while still carrying CORS headers (browsers otherwise report
      an opaque CORS failure instead of the error).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def guarded_send(message: Any) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except _BodyTooLarge:
            # Let the body cap's signal reach BodySizeLimitMiddleware so it
            # emits a clean 413 instead of being rendered/masked here.
            raise
        except Exception as exc:
            if response_started:
                raise
            # Exceptions raised by a route are already rendered by Starlette's
            # ExceptionMiddleware; one that reaches a middleware came from
            # ANOTHER middleware (above ExceptionMiddleware) and would reach
            # ServerErrorMiddleware as a 500: render it with the framework
            # handlers, keeping its status.
            response: Response
            if isinstance(exc, StarletteHTTPException):
                response = http_exception_handler(Request(scope), exc)
            elif isinstance(exc, RequestValidationError):
                response = validation_exception_handler(Request(scope), exc)
            elif (declared := http_status_of(exc)) is not None:
                response = protocol_response(exc, declared)
            else:
                logger.error(
                    "Middleware caught: %s", describe_exception(exc), exc_info=True
                )
                _report_unhandled(scope, exc)
                response = JSONResponse(
                    status_code=500, content=dict(_INTERNAL_ERROR_BODY)
                )
            await response(scope, receive, send)


class RouteErrorMiddleware(Mask5xxMiddleware):
    """The innermost placement of :class:`Mask5xxMiddleware` (see there)."""


def install_mask5xx(app: FastAPI) -> None:
    """Add the 5xx mask once (idempotent across both registration helpers)."""
    if any(cast(Any, m.cls) is Mask5xxMiddleware for m in app.user_middleware):
        return
    app.add_middleware(Mask5xxMiddleware)


def register_error_handlers(app: FastAPI) -> None:
    """Install the framework error handlers + 5xx masking middleware.

    The mask is normally placed by ``register_middlewares`` (inside CORS); when
    this runs first (or alone) it installs it here, and ``register_middlewares``
    then re-positions it.
    """

    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(MissingTenantError, missing_tenant_handler)
    # Idempotency replay: a retry of a previously-succeeded keyed request is
    # converted back into the stored response (handled before the catch-all).
    app.add_exception_handler(_replay._ReplayResponse, _replay._replay_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
    install_mask5xx(app)
