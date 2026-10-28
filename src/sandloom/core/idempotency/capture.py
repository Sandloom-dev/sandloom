"""Response capture: settle an armed request's cache reservation.

A FastAPI dependency cannot see the response, so the cache guard only *arms*
a fresh first request; :class:`IdempotencyCaptureMiddleware` captures that
request's response and writes the terminal ``succeeded`` record — or releases
the key after a failed attempt — through the reservation lifecycle
(:mod:`sandloom.core.idempotency.reservation`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from sandloom.core import effects
from sandloom.core.idempotency.fingerprint import _ARMED_ATTR, IdempotencyReservation
from sandloom.core.idempotency.replay import success_record
from sandloom.core.idempotency.reservation import _persist_terminal, _release_key

logger = logging.getLogger("sandloom.core.idempotency")


# Upper bound on a captured response body we are willing to base64-encode and
# store in the cache for replay. A larger response is still returned to the
# original caller, but its terminal record stores a non-replayable marker (the
# status code only) instead of the body — so a single large/streaming armed
# endpoint cannot bloat the cache, and a retry still cannot re-execute the
# (already succeeded) side effect.
_MAX_CAPTURED_RESPONSE_BYTES = effects.RESULT_MAX_BYTES


# Terminal-record writes scheduled from an aborted response stream (client
# disconnect / cancellation). Kept referenced so they are not garbage-collected
# mid-flight; they run detached from the (cancelled) request task.
_background_persists: set[asyncio.Task[Any]] = set()


def _persist_detached(
    reservation: IdempotencyReservation, record: dict[str, Any]
) -> None:
    try:
        task = asyncio.get_running_loop().create_task(
            _persist_terminal(reservation, record)
        )
    except RuntimeError:  # pragma: no cover - no loop (interpreter shutdown)
        return
    _background_persists.add(task)
    task.add_done_callback(_background_persists.discard)


class IdempotencyCaptureMiddleware:
    """Capture an armed request's response and write its terminal record.

    Pure ASGI (not ``BaseHTTPMiddleware``): it runs inline in the request task
    and wraps only ``send``, so an endpoint exception propagates UNCHANGED to
    the outer error handling (no anyio task group wrapping it in an
    ``ExceptionGroup``) and streaming / background tasks behave exactly as
    without the middleware.

    Only requests the guard armed (a fresh first request) are touched — every
    other request passes through completely untouched. A 2xx/3xx response is
    streamed through to the client UNMODIFIED (original status, raw headers
    including duplicate ``Set-Cookie``, and background tasks preserved) while its
    body is tee'd into a terminal ``succeeded`` record for best-effort replay; a
    response whose body exceeds the cap becomes a non-replayable success marker
    rather than being buffered whole. A >=400 response (or an exception before
    the response started) releases the key so a legitimate retry can reserve
    afresh.

    The success record is written even when the response stream is ABORTED
    (client disconnect, cancellation, a body iterator error after the 2xx
    status went out): the endpoint already ran, so a retry must never re-execute
    it once the short pending TTL lapses. Such a record is non-replayable
    (status only) because its body is incomplete.

    The tee accumulates at most ``_MAX_CAPTURED_RESPONSE_BYTES`` (dropping the
    partial buffer once exceeded), so a large or streaming response is never
    fully buffered in memory.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # ``request.state`` is backed by ``scope["state"]``; make sure the dict
        # exists up-front so the guard's arming is visible to us by reference.
        state: dict[str, Any] = scope.setdefault("state", {})
        capture = _ResponseCapture(state)

        async def _send(message: Any) -> None:
            await capture.before_send(message)
            await send(message)
            await capture.after_send(message)

        try:
            await self.app(scope, receive, _send)
        except BaseException:
            await capture.on_abort()
            raise
        await capture.on_abort()  # no-op unless the stream never completed


class _ResponseCapture:
    """Per-request state machine behind :class:`IdempotencyCaptureMiddleware`."""

    __slots__ = (
        "_state",
        "armed",
        "mode",
        "status",
        "headers",
        "media_type",
        "captured",
        "overflow",
        "done",
    )

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self.armed: IdempotencyReservation | None = None
        # "idle" (no response yet) | "pass" (not ours) | "capture" | "done"
        self.mode = "idle"
        self.status = 200
        self.headers: dict[str, str] = {}
        self.media_type: str | None = None
        self.captured = bytearray()
        self.overflow = False
        self.done = False

    def _armed_now(self) -> IdempotencyReservation | None:
        armed = self._state.get(_ARMED_ATTR)
        return cast("IdempotencyReservation | None", armed)

    async def before_send(self, message: Any) -> None:
        kind = message.get("type")
        if kind == "http.response.start":
            armed = self._armed_now()
            if armed is None:
                self.mode = "pass"
                return
            self.armed = armed
            self.status = int(message.get("status", 200))
            if self.status >= 400 or armed.in_flight_only:
                # Failed attempt (or an in-flight-only guard): release the key
                # (a retry reserves afresh via atomic SET NX); the ORIGINAL
                # response streams straight through, nothing is stored.
                # Send the original response before paying cache-outage retry
                # time, just as successful terminal persistence runs after it.
                self.mode = "release"
                return
            self.mode = "capture"
            raw = message.get("headers") or []
            self.headers = {
                bytes(k).decode("latin-1"): bytes(v).decode("latin-1") for k, v in raw
            }
            ctype = next(
                (v for k, v in self.headers.items() if k.lower() == "content-type"),
                None,
            )
            self.media_type = ctype.split(";", 1)[0].strip() if ctype else None
        elif kind == "http.response.body" and self.mode == "capture":
            data = bytes(message.get("body", b"") or b"")
            if not self.overflow:
                if len(self.captured) + len(data) <= _MAX_CAPTURED_RESPONSE_BYTES:
                    self.captured.extend(data)
                else:
                    # Too large to store for replay: drop the partial buffer so
                    # memory stays bounded; the body still streams out.
                    self.overflow = True
                    self.captured = bytearray()

    async def after_send(self, message: Any) -> None:
        if (
            self.mode == "release"
            and message.get("type") == "http.response.body"
            and not message.get("more_body", False)
        ):
            self.done = True
            self.mode = "done"
            if self.armed is not None:
                await _release_key(self.armed)
            return
        if (
            self.mode == "capture"
            and message.get("type") == "http.response.body"
            and not message.get("more_body", False)
        ):
            self.mode = "done"
            self.done = True
            if self.armed is not None:
                await self._persist_success(self.armed)

    async def on_abort(self) -> None:
        """Called when the app finished or raised: settle an unsettled key."""
        if self.done:
            return
        if self.mode == "release" and self.armed is not None:
            self.done = True
            await _release_key(self.armed)
            return
        if self.mode == "idle":
            # No response went out. If the guard armed the key, the endpoint (or
            # a dependency) raised: release it so a retry may reserve afresh.
            armed = self._armed_now()
            self.done = True
            if armed is not None:
                await _release_key(armed)
            return
        if self.mode == "capture" and self.armed is not None:
            # Stream aborted after a success status: never leave only the
            # short-lived pending record (its expiry would let a retry
            # re-execute the operation). Detached: we may be cancelled.
            self.done = True
            logger.warning(
                "Idempotent response stream for %s aborted; storing a "
                "non-replayable success marker",
                self.armed.full_key,
            )
            _persist_detached(
                self.armed,
                success_record(
                    self.armed,
                    status_code=self.status,
                    body=b"",
                    headers={},
                    media_type=None,
                    replayable=False,
                ),
            )

    async def _persist_success(self, armed: IdempotencyReservation) -> None:
        if self.overflow:
            logger.warning(
                "Idempotent response for %s exceeds %d bytes; storing a "
                "non-replayable terminal marker",
                armed.full_key,
                _MAX_CAPTURED_RESPONSE_BYTES,
            )
        record = success_record(
            armed,
            status_code=self.status,
            body=bytes(self.captured),
            headers=self.headers,
            media_type=self.media_type,
            replayable=not self.overflow,
        )
        await _persist_terminal(armed, record)
