"""The fused HTTP layer preserves the standalone composition's wire contract."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.http.middlewares import (
    ProcessTimeMiddleware,
    RequestIDMiddleware,
    SandloomPipelineMiddleware,
    SecurityHeadersMiddleware,
    request_id_var,
)
from sandloom.core.idempotency import IdempotencyCaptureMiddleware


def _legacy(app: Any) -> Any:
    return RequestIDMiddleware(
        SecurityHeadersMiddleware(
            IdempotencyCaptureMiddleware(ProcessTimeMiddleware(app))
        )
    )


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.parametrize("status", [200, 204, 401, 403, 404, 413, 422, 500, 503])
@pytest.mark.parametrize("production", [False, True])
@pytest.mark.parametrize(
    "incoming", [None, b"", b"caller-id", b"x" * 200, b"bad\x01id"]
)
async def test_pipeline_matches_legacy_headers_stream_and_context(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    production: bool,
    incoming: bytes | None,
) -> None:
    monkeypatch.setattr(settings, "environment", "prod" if production else "test")
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=123))
    seen = []
    outer = request_id_var.set("outer-context")
    try:
        for factory in (_legacy, SandloomPipelineMiddleware):
            sent: list[dict[str, Any]] = []
            context: list[str | None] = []

            async def app(
                scope: Any, receive: Any, send: Any, context: list[str | None] = context
            ) -> None:
                context.append(request_id_var.get())
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"set-cookie", b"a=1"),
                            (b"set-cookie", b"b=2"),
                            (b"cache-control", b"private"),
                            (b"x-frame-options", b"SAMEORIGIN"),
                            (b"x-request-id", b"handler-id"),
                            (b"x-request-id", b"duplicate-id"),
                            (b"x-process-time", b"handler-time"),
                            (b"strict-transport-security", b"max-age=123"),
                        ],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b"first", "more_body": True}
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"second",
                        "more_body": False,
                    }
                )
                context.append(request_id_var.get())  # streaming/background work

            async def send(message: Any, sent: list[dict[str, Any]] = sent) -> None:
                sent.append(message)

            scope = {
                "type": "http",
                "method": "GET",
                "headers": [] if incoming is None else [(b"x-request-id", incoming)],
            }
            await factory(app)(scope, _receive, send)
            assert request_id_var.get() == "outer-context"
            raw = sent[0]["headers"]
            elapsed = [v for k, v in raw if k == b"x-process-time"]
            assert len(elapsed) == 1 and float(elapsed[0]) >= 0
            sent[0]["headers"] = [(k, v) for k, v in raw if k != b"x-process-time"]
            assert [v for k, v in raw if k == b"set-cookie"] == [b"a=1", b"b=2"]
            seen.append((sent, context))
        assert seen[0] == seen[1]
    finally:
        request_id_var.reset(outer)


@pytest.mark.parametrize("production", [False, True])
async def test_pipeline_adds_defaults(
    monkeypatch: pytest.MonkeyPatch, production: bool
) -> None:
    monkeypatch.setattr(settings, "environment", "prod" if production else "test")
    sent = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: Any) -> None:
        sent.append(message)

    await SandloomPipelineMiddleware(app)(
        {"type": "http", "method": "GET"}, _receive, send
    )
    headers = dict(sent[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"cache-control"] == b"no-store"
    assert (b"strict-transport-security" in headers) == production
    assert uuid.UUID(headers[b"x-request-id"].decode()).version == 4


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("exception", [RuntimeError, asyncio.CancelledError])
async def test_pipeline_propagates_errors_and_restores_context(
    started: bool, exception: Any
) -> None:
    failure = exception("interrupted")
    sent = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if started:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise failure

    async def send(message: Any) -> None:
        sent.append(message)

    outer = request_id_var.set("outer")
    try:
        with pytest.raises(exception) as caught:
            await SandloomPipelineMiddleware(app)(
                {"type": "http", "method": "GET"}, _receive, send
            )
        assert caught.value is failure
        assert request_id_var.get() == "outer"
        assert len(sent) == int(started)  # never synthesize another response
    finally:
        request_id_var.reset(outer)


@pytest.mark.parametrize("kind", ["websocket", "lifespan"])
async def test_pipeline_non_http_is_untouched(kind: str) -> None:
    scope = {"type": kind}

    async def send(message: Any) -> None:
        pass

    async def app(actual: Any, receive: Any, actual_send: Any) -> None:
        assert actual is scope and receive is _receive and actual_send is send
        assert request_id_var.get() == "outer"

    outer = request_id_var.set("outer")
    try:
        await SandloomPipelineMiddleware(app)(scope, _receive, send)
    finally:
        request_id_var.reset(outer)


async def test_pipeline_request_context_isolated_between_tasks() -> None:
    arrived = asyncio.Event()
    count = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal count
        expected = scope["headers"][0][1].decode()
        count += 1
        if count == 2:
            arrived.set()
        await arrived.wait()
        assert request_id_var.get() == expected
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: Any) -> None:
        pass

    pipeline = SandloomPipelineMiddleware(app)
    await asyncio.gather(
        *(
            pipeline(
                {"type": "http", "method": "GET", "headers": [(b"x-request-id", name)]},
                _receive,
                send,
            )
            for name in (b"first", b"second")
        )
    )


_HEADER_CASES: list[list[tuple[bytes, bytes]]] = [
    [],
    [(b"content-type", b"application/json")],
    [(b"Content-Type", b"application/problem+json; charset=utf-8")],
    [(b"content-type", b"text/html"), (b"content-type", b"application/json")],
    [
        (b"X-Request-ID", b"a"),
        (b"x-process-time", b"1"),
        (b"X-Process-Time", b"2"),
        (b"x-request-id", b"b"),
        (b"Content-Security-Policy", b"mine"),
        (b"content-type", b"application/json"),
        (b"X-Frame-Options", b"SAMEORIGIN"),
        (b"strict-transport-security", b"max-age=1"),
    ],
    [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2"), (b"cache-control", b"x")],
]


@pytest.mark.parametrize("headers", _HEADER_CASES)
@pytest.mark.parametrize("production", [False, True])
def test_single_header_pass_matches_the_sequential_composition(
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[bytes, bytes]],
    production: bool,
) -> None:
    from sandloom.core.http.middlewares import (
        _PROCESS_TIME_HEADER,
        _REQUEST_ID_HEADER_BYTES,
        _add_security_headers,
        _finish_response_headers,
        _set_raw_header,
    )

    monkeypatch.setattr(settings, "environment", "prod" if production else "test")
    expected = list(headers)
    _set_raw_header(expected, _PROCESS_TIME_HEADER, b"0.5")
    _add_security_headers(expected)
    _set_raw_header(expected, _REQUEST_ID_HEADER_BYTES, b"rid")
    actual = list(headers)
    _finish_response_headers(actual, b"0.5", b"rid")
    assert actual == expected


async def test_unarmed_request_builds_no_idempotency_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.idempotency import capture as idempotency_capture

    def _no_capture(state: Any) -> Any:
        raise AssertionError("capture built for an unarmed request")

    monkeypatch.setattr(idempotency_capture, "_ResponseCapture", _no_capture)
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: Any) -> None:
        sent.append(message)

    await SandloomPipelineMiddleware(app)(
        {"type": "http", "method": "GET"}, _receive, send
    )
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


@pytest.mark.parametrize("started", [False, True])
async def test_armed_request_settles_its_key_like_the_capture(
    monkeypatch: pytest.MonkeyPatch, started: bool
) -> None:
    from sandloom.core.idempotency import capture as idempotency_capture
    from sandloom.core.idempotency import fingerprint as idempotency_fingerprint

    assert idempotency_fingerprint._ARMED_ATTR == "_sandloom_idempotency"
    released: list[Any] = []

    async def _release(reservation: Any) -> None:
        released.append(reservation)

    monkeypatch.setattr(idempotency_capture, "_release_key", _release)
    armed = idempotency_fingerprint.IdempotencyReservation(
        full_key="idem:x", fingerprint="fp", ttl_seconds=60, pending_value="p"
    )

    async def app(scope: Any, receive: Any, send: Any) -> None:
        scope["state"][idempotency_fingerprint._ARMED_ATTR] = armed
        if started:
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"x"})
            return
        raise RuntimeError("endpoint failed before responding")

    async def send(message: Any) -> None:
        pass

    if started:
        await SandloomPipelineMiddleware(app)(
            {"type": "http", "method": "POST"}, _receive, send
        )
    else:
        with pytest.raises(RuntimeError):
            await SandloomPipelineMiddleware(app)(
                {"type": "http", "method": "POST"}, _receive, send
            )
    assert released == [armed]
