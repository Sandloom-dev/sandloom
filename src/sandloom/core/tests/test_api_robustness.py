"""API robustness: body-size limit, docs gating, idempotency guard."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from sandloom.core.config.settings import settings
from sandloom.core.di import idempotency_guard
from sandloom.core.http.middlewares import register_middlewares


def test_body_size_limit_rejects_oversized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_request_body_bytes", 100, raising=False)
    app = FastAPI()
    register_middlewares(app)

    @app.post("/echo")
    async def _echo(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    client = TestClient(app)
    big = {"data": "x" * 1000}
    resp = client.post("/echo", json=big)
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_entity_too_large"


def test_body_size_limit_allows_small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_request_body_bytes", 10_000, raising=False)
    app = FastAPI()
    register_middlewares(app)

    @app.post("/echo")
    async def _echo(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    client = TestClient(app)
    resp = client.post("/echo", json={"ok": True})
    assert resp.status_code == 200


def test_docs_exposed_property(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.config.common import Environment

    monkeypatch.setattr(settings, "docs_enabled", None, raising=False)
    monkeypatch.setattr(settings, "environment", Environment.prod, raising=False)
    assert settings.docs_exposed is False
    monkeypatch.setattr(settings, "environment", Environment.dev, raising=False)
    assert settings.docs_exposed is True
    monkeypatch.setattr(settings, "docs_enabled", True, raising=False)
    monkeypatch.setattr(settings, "environment", Environment.prod, raising=False)
    assert settings.docs_exposed is True


@pytest.fixture(autouse=True)
def _restore_cache_singleton() -> Any:
    """Save/restore the driver's default cache client so tests don't leak it."""
    import sandloom.core.drivers.redis_cache as redis_cache

    saved = redis_cache._default_client
    yield
    redis_cache._default_client = saved


class _FakeRedis:
    """Minimal GET / SET NX EX semantics for idempotency tests."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self._store.get(name)

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self._store:
            return None
        self._store[name] = value
        return True

    async def delete(self, name: str) -> int:
        return 1 if self._store.pop(name, None) is not None else 0


def _idempotency_app(fake: _FakeRedis, *, counter: dict[str, int]) -> FastAPI:
    from sandloom.core.http.error_handlers import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    register_middlewares(app)

    # Inject the fake at the driver's single construction seam: di.cache()'s
    # default provider returns this client.
    import sandloom.core.drivers.redis_cache as redis_cache

    redis_cache._default_client = fake  # type: ignore[assignment]

    @app.post("/charge", dependencies=[Depends(idempotency_guard(scope="charge"))])
    async def _charge() -> dict[str, Any]:
        counter["n"] += 1
        return {"status": "ok", "n": counter["n"]}

    return app


def test_idempotency_replays_successful_response() -> None:
    fake = _FakeRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))

    # An identified caller (anonymous callers never get a body replayed —
    # see test_idempotency_anonymous_retry_is_deduped_without_body_replay).
    headers = {"Idempotency-Key": "abc-123", "Authorization": "Bearer client-1"}
    first = client.post("/charge", headers=headers)
    assert first.status_code == 200
    assert first.json() == {"status": "ok", "n": 1}
    # Same key again -> the stored response is replayed, the handler does NOT
    # run again (n stays 1), and the replay is flagged.
    second = client.post("/charge", headers=headers)
    assert second.status_code == 200
    assert second.json() == {"status": "ok", "n": 1}
    assert second.headers.get("Idempotent-Replay") == "true"
    assert counter["n"] == 1


def test_idempotency_anonymous_retry_is_deduped_without_body_replay() -> None:
    """Low: anonymous identity is only the source IP (shared behind a proxy /
    NAT), so an anonymous caller is deduplicated but never handed a stored body
    — one client cannot read another's response by reusing its key."""
    fake = _FakeRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))

    headers = {"Idempotency-Key": "anon-key"}
    first = client.post("/charge", headers=headers)
    assert first.json() == {"status": "ok", "n": 1}
    second = client.post("/charge", headers=headers)
    assert second.status_code == 200
    assert second.content == b""  # body withheld
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.headers.get("Idempotent-Replay-Truncated") == "true"
    assert counter["n"] == 1  # still never re-executed


def test_idempotency_corrupt_record_does_not_block_key() -> None:
    """Low: an unparseable record must not 409 the key for its whole TTL."""
    fake = _FakeRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))
    headers = {"Idempotency-Key": "corrupt", "Authorization": "Bearer client-1"}
    first = client.post("/charge", headers=headers)
    assert first.status_code == 200
    for key in list(fake._store):
        fake._store[key] = "{not json"
    retry = client.post("/charge", headers=headers)
    assert retry.status_code == 200
    assert retry.json() == {"status": "ok", "n": 2}


def test_idempotency_never_replays_across_tenants() -> None:
    fake = _FakeRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))

    tenant_a = "11111111-1111-1111-1111-111111111111"
    tenant_b = "22222222-2222-2222-2222-222222222222"
    key = "shared-client-key"

    first = client.post(
        "/charge",
        headers={
            "Idempotency-Key": key,
            "X-Tenant-Id": tenant_a,
            "Authorization": "Bearer client-1",
        },
    )
    assert first.json() == {"status": "ok", "n": 1}

    # The same caller and client-generated key in another tenant is a distinct
    # operation. It must execute, never receive tenant A's stored response.
    second = client.post(
        "/charge",
        headers={
            "Idempotency-Key": key,
            "X-Tenant-Id": tenant_b,
            "Authorization": "Bearer client-1",
        },
    )
    assert second.json() == {"status": "ok", "n": 2}
    assert second.headers.get("Idempotent-Replay") is None

    # A real retry inside tenant B still replays normally.
    retry = client.post(
        "/charge",
        headers={
            "Idempotency-Key": key,
            "X-Tenant-Id": tenant_b,
            "Authorization": "Bearer client-1",
        },
    )
    assert retry.json() == {"status": "ok", "n": 2}
    assert retry.headers.get("Idempotent-Replay") == "true"
    assert counter["n"] == 2


def test_idempotency_pending_request_conflicts() -> None:
    fake = _FakeRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))
    # Pre-seed a pending record (a first request still in flight) for this key.
    # Keys are scoped to the caller identity; with no Authorization header the
    # caller is "anon:<source-ip>" (TestClient's peer host is "testclient"), so
    # build the same caller-scoped key the guard computes.
    import hashlib
    import json

    caller_tag = hashlib.sha256(b"anon:testclient").hexdigest()[:16]
    tenant_tag = hashlib.sha256(b"t:none").hexdigest()[:16]
    fake._store[f"idem:charge:{caller_tag}:{tenant_tag}:in-flight"] = json.dumps(
        {"state": "pending", "fp": None}
    )
    resp = client.post("/charge", headers={"Idempotency-Key": "in-flight"})
    assert resp.status_code == 409
    assert counter["n"] == 0  # handler never ran


def test_idempotency_rejects_key_reused_with_different_body() -> None:
    fake = _FakeRedis()
    counter = {"n": 0}

    from sandloom.core.http.error_handlers import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    register_middlewares(app)
    import sandloom.core.drivers.redis_cache as redis_cache

    redis_cache._default_client = fake  # type: ignore[assignment]

    @app.post("/op", dependencies=[Depends(idempotency_guard(scope="op"))])
    async def _op(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        return payload

    client = TestClient(app)
    headers = {"Idempotency-Key": "same-key"}
    first = client.post("/op", headers=headers, json={"a": 1})
    assert first.status_code == 200
    # Same key, different body -> client misuse -> 422.
    second = client.post("/op", headers=headers, json={"a": 2})
    assert second.status_code == 422


def test_idempotency_failed_attempt_allows_retry() -> None:
    fake = _FakeRedis()
    state = {"fail_first": True}

    from sandloom.core.http.error_handlers import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    register_middlewares(app)
    import sandloom.core.drivers.redis_cache as redis_cache

    redis_cache._default_client = fake  # type: ignore[assignment]

    @app.post("/flaky", dependencies=[Depends(idempotency_guard(scope="flaky"))])
    async def _flaky() -> dict[str, str]:
        from fastapi import HTTPException

        if state["fail_first"]:
            state["fail_first"] = False
            raise HTTPException(status_code=502, detail="downstream down")
        return {"status": "ok"}

    client = TestClient(app)
    headers = {"Idempotency-Key": "retry-me"}
    first = client.post("/flaky", headers=headers)
    assert first.status_code == 502
    # The failed attempt did NOT poison the key for the whole TTL — retry runs.
    second = client.post("/flaky", headers=headers)
    assert second.status_code == 200
    assert second.json() == {"status": "ok"}


def test_idempotency_guard_requires_key() -> None:
    fake = _FakeRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))
    resp = client.post("/charge")  # no Idempotency-Key header
    assert resp.status_code == 400


class _TtlRecordingRedis(_FakeRedis):
    """Records the EX (ttl) used for every SET, keyed by name + state."""

    def __init__(self) -> None:
        super().__init__()
        self.ttls: list[tuple[str, int | None]] = []

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        ok = await super().set(name, value, nx=nx, ex=ex)
        if ok:
            import json

            state = json.loads(value).get("state")
            self.ttls.append((state, ex))
        return ok


def test_idempotency_pending_reservation_uses_short_ttl() -> None:
    """The in-flight reservation self-heals fast; the terminal record is long.

    A request that dies before writing a terminal record must not block retries
    for the full TTL, so the ``pending`` reservation gets a short cap while the
    ``succeeded`` record keeps the full ``ttl_seconds``.
    """
    from sandloom.core.idempotency.reservation import _PENDING_RESERVATION_TTL_SECONDS

    fake = _TtlRecordingRedis()
    counter = {"n": 0}
    client = TestClient(_idempotency_app(fake, counter=counter))
    resp = client.post("/charge", headers={"Idempotency-Key": "ttl-key"})
    assert resp.status_code == 200

    by_state = dict(fake.ttls)
    # The default guard TTL is 24h; the pending reservation is capped short.
    assert by_state["pending"] == _PENDING_RESERVATION_TTL_SECONDS
    assert by_state["succeeded"] == 24 * 60 * 60


class _BrokenRedis:
    """A cache client whose every op fails (simulates an outage)."""

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cache down")

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cache down")


def _pay_app(*, fail_closed: bool) -> FastAPI:
    app = FastAPI()
    import sandloom.core.drivers.redis_cache as redis_cache

    redis_cache._default_client = _BrokenRedis()  # type: ignore[assignment]

    @app.post(
        "/pay",
        dependencies=[Depends(idempotency_guard(scope="pay", fail_closed=fail_closed))],
    )
    async def _pay() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_idempotency_fail_closed_returns_503_on_cache_outage() -> None:
    client = TestClient(_pay_app(fail_closed=True))
    resp = client.post("/pay", headers={"Idempotency-Key": "k1"})
    # Money endpoint: a cache outage must NOT let the request through.
    assert resp.status_code == 503


def test_idempotency_fail_open_allows_on_cache_outage() -> None:
    client = TestClient(_pay_app(fail_closed=False))
    resp = client.post("/pay", headers={"Idempotency-Key": "k1"})
    # Default non-critical endpoint: availability beats dedup.
    assert resp.status_code == 200


# --- idempotency response-capture middleware --------------------------------
def _capture_app(fake: _FakeRedis) -> FastAPI:
    """App whose handlers exercise the capture middleware's edge cases."""
    from sandloom.core.http.error_handlers import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    register_middlewares(app)
    import sandloom.core.drivers.redis_cache as redis_cache

    redis_cache._default_client = fake  # type: ignore[assignment]
    return app


def test_idempotency_large_response_returns_full_body_stores_marker() -> None:
    from fastapi import Response

    from sandloom.core.idempotency.capture import _MAX_CAPTURED_RESPONSE_BYTES

    fake = _FakeRedis()
    counter = {"n": 0}
    app = _capture_app(fake)
    big = "y" * (_MAX_CAPTURED_RESPONSE_BYTES + 1024)

    @app.post("/big", dependencies=[Depends(idempotency_guard(scope="big"))])
    async def _big() -> Response:
        counter["n"] += 1
        return Response(content=big, media_type="text/plain")

    client = TestClient(app)
    headers = {"Idempotency-Key": "big-1"}
    first = client.post("/big", headers=headers)
    # The oversized body is streamed to the client IN FULL (not truncated)...
    assert first.status_code == 200
    assert first.text == big
    assert counter["n"] == 1
    # ...but a retry replays a non-replayable success marker (handler NOT re-run).
    second = client.post("/big", headers=headers)
    assert counter["n"] == 1
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.headers.get("Idempotent-Replay-Truncated") == "true"


def test_idempotency_capture_preserves_duplicate_set_cookie() -> None:
    from fastapi import Response

    fake = _FakeRedis()
    app = _capture_app(fake)

    @app.post("/cookies", dependencies=[Depends(idempotency_guard(scope="cookies"))])
    async def _cookies() -> Response:
        resp = Response(content="ok", media_type="text/plain")
        resp.set_cookie("a", "1")
        resp.set_cookie("b", "2")
        return resp

    client = TestClient(app)
    resp = client.post("/cookies", headers={"Idempotency-Key": "c1"})
    assert resp.status_code == 200
    # Both Set-Cookie headers survive the capture passthrough (dict(headers)
    # would have collapsed them to one).
    assert resp.cookies.get("a") == "1"
    assert resp.cookies.get("b") == "2"


def test_idempotency_unhandled_exception_releases_key() -> None:
    fake = _FakeRedis()
    state = {"fail_first": True}
    app = _capture_app(fake)

    @app.post("/boom", dependencies=[Depends(idempotency_guard(scope="boom"))])
    async def _boom() -> dict[str, str]:
        if state["fail_first"]:
            state["fail_first"] = False
            raise RuntimeError("unhandled error")
        return {"status": "ok"}

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Idempotency-Key": "boom-1"}
    first = client.post("/boom", headers=headers)
    assert first.status_code == 500
    # The middleware released the key despite the unhandled exception, so a
    # retry is allowed to proceed instead of being stuck at 409 for the TTL.
    second = client.post("/boom", headers=headers)
    assert second.status_code == 200
    assert second.json() == {"status": "ok"}


async def test_idempotency_capture_is_pure_asgi_and_never_wraps_errors() -> None:
    """The capture middleware is pure ASGI: an endpoint exception reaches the
    outer error handling as itself, never wrapped in an ExceptionGroup."""
    from starlette.middleware.base import BaseHTTPMiddleware

    from sandloom.core.idempotency import IdempotencyCaptureMiddleware

    assert not issubclass(IdempotencyCaptureMiddleware, BaseHTTPMiddleware)

    class _Boom(Exception):
        pass

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        raise _Boom("endpoint failed")

    async def _receive() -> Any:  # pragma: no cover - never read
        return {"type": "http.request", "body": b""}

    async def _send(_message: Any) -> None:  # pragma: no cover - nothing sent
        return None

    middleware = IdempotencyCaptureMiddleware(_app)
    with pytest.raises(_Boom):
        await middleware({"type": "http", "state": {}}, _receive, _send)


def test_idempotency_capture_keeps_background_and_replays() -> None:
    """Background tasks still run and the captured body replays verbatim."""
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse

    fake = _FakeRedis()
    app = _capture_app(fake)
    ran: list[str] = []

    @app.post("/bg", dependencies=[Depends(idempotency_guard(scope="bg"))])
    async def _bg() -> JSONResponse:
        return JSONResponse(
            {"n": len(ran)},
            status_code=201,
            background=BackgroundTask(lambda: ran.append("x")),
        )

    client = TestClient(app)
    headers = {"Idempotency-Key": "bg-1", "Authorization": "Bearer x"}
    first = client.post("/bg", headers=headers)
    assert first.status_code == 201
    assert first.json() == {"n": 0}
    assert ran == ["x"]
    second = client.post("/bg", headers=headers)
    assert second.status_code == 201
    assert second.json() == {"n": 0}
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.headers.get("content-type", "").startswith("application/json")
    assert ran == ["x"]  # a replay never re-executes the endpoint


async def _run_body_cap(headers: list[tuple[bytes, bytes]], body: bytes) -> int:
    from sandloom.core.http.middlewares import BodySizeLimitMiddleware

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        # Does not read the body: only the fast path can reject.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    await BodySizeLimitMiddleware(app, max_bytes=100)(scope, receive, send)
    return int(sent[0]["status"])  # type: ignore[call-overload]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([b"10", b"999999"], 400),  # conflicting: smuggling-shaped, refuse
        ([b"999999", b"10"], 400),
        ([b"10", b"10"], 200),  # identical duplicates are equivalent to one
        ([b"999999", b"999999"], 413),
        ([b"10"], 200),
        ([b"999999"], 413),
    ],
)
async def test_body_cap_fast_path_considers_every_content_length(
    values: list[bytes], expected: int
) -> None:
    headers = [(b"content-length", v) for v in values]
    assert await _run_body_cap(headers, b"x" * 10) == expected
