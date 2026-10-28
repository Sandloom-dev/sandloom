"""Bounded Redis cache client: an unreachable / hung Redis RAISES quickly.

Before: the client had no socket/connect timeout and an unbounded pool, so a
blackholed or hung Redis made every cache call await forever — the
fail-open / fail-closed branches of the rate limiter, token revocation,
idempotency and login lockout never ran; the request just hung. Now every
operation raises within ~(1 + retries) x CACHE_SOCKET_TIMEOUT_SECONDS and each
caller's policy branch runs. Plus the fused single-round-trip operations
(``incr_window`` / ``incr_many`` / ``set_nx_get``) against the real test Redis.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException

from sandloom.core.config.settings import settings
from sandloom.core.drivers import redis_cache
from sandloom.core.drivers.redis_cache import RedisCacheProvider

#: Generous wall-clock bound (the configured timeout is 0.5s): a regression to
#: an unbounded client hangs until the outer ``wait_for`` guard instead.
_BOUND_SECONDS = 4.0
_HANG_GUARD_SECONDS = 15.0


@pytest_asyncio.fixture
async def hung_redis_url() -> AsyncIterator[str]:
    """A TCP server that accepts connections and never answers (hung Redis)."""
    held: list[asyncio.StreamWriter] = []

    async def _accept(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)  # keep the socket open, never reply

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()


def _closed_port_url() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens there now
    return f"redis://127.0.0.1:{port}/0"


def _provider(url: str) -> RedisCacheProvider:
    return RedisCacheProvider(
        name="bounded", driver="redis", options={"url": url}, shared=False
    )


async def _elapsed_raise(coro: Any) -> tuple[float, BaseException]:
    start = time.monotonic()
    try:
        await asyncio.wait_for(coro, timeout=_HANG_GUARD_SECONDS)
    except TimeoutError:
        pytest.fail("cache operation hung (no client timeout)")
    except Exception as exc:  # noqa: BLE001 - the error under test
        return time.monotonic() - start, exc
    pytest.fail("cache operation against a dead Redis did not raise")


# --------------------------------------------------------------------------- #
# Client construction
# --------------------------------------------------------------------------- #
def test_client_is_bounded() -> None:
    client = redis_cache._build_client("redis://127.0.0.1:6399/0")
    pool = client.connection_pool
    kwargs = pool.connection_kwargs
    assert type(pool).__name__ == "BlockingConnectionPool"
    assert pool.max_connections == settings.cache_max_connections
    assert kwargs["socket_timeout"] == settings.cache_socket_timeout_seconds
    assert (
        kwargs["socket_connect_timeout"]
        == settings.cache_socket_connect_timeout_seconds
    )
    assert kwargs["socket_keepalive"] is True
    assert kwargs["health_check_interval"] == 30
    retry = kwargs["retry"]
    assert retry._retries == settings.cache_retry_attempts
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    # Connection errors are retried, timeouts never (they would multiply).
    assert RedisConnectionError in retry._supported_errors
    assert RedisTimeoutError not in retry._supported_errors


async def test_transport_client_has_no_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event bus gets a sibling client: blocking XREADGROUP / pub-sub
    reads must not trip the cache's short socket timeout."""
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)
    monkeypatch.setattr(redis_cache, "_default_transport", {})
    monkeypatch.setattr(redis_cache.settings, "cache_url", "redis://127.0.0.1:6399/0")
    try:
        main = await redis_cache.get_default_client()
        transport = await redis_cache.get_default_transport_client()
        assert transport is not main
        assert transport.connection_pool.connection_kwargs.get("socket_timeout") is None
        assert await redis_cache.get_default_transport_client() is transport
    finally:
        await redis_cache.close_default_client()


async def test_injected_default_client_is_shared_with_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = object()

    async def _fake() -> Any:
        return fake

    monkeypatch.setattr(redis_cache, "get_default_client", _fake)
    assert await redis_cache.get_default_transport_client() is fake


# --------------------------------------------------------------------------- #
# Dead Redis -> operations raise within the bound
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("op", ["get", "incr", "add", "incr_window", "set_nx_get"])
async def test_hung_redis_raises_within_timeout(hung_redis_url: str, op: str) -> None:
    provider = _provider(hung_redis_url)
    calls = {
        "get": lambda: provider.get("k"),
        "incr": lambda: provider.incr("k", ttl_seconds=5),
        "add": lambda: provider.add("k", "v", ttl_seconds=5),
        "incr_window": lambda: provider.incr_window("k", "p", ttl_seconds=5),
        "set_nx_get": lambda: provider.set_nx_get("k", "v", ttl_seconds=5),
    }
    try:
        elapsed, _exc = await _elapsed_raise(calls[op]())
    finally:
        await provider.aclose()
    assert elapsed < _BOUND_SECONDS


async def test_closed_port_raises_fast() -> None:
    provider = _provider(_closed_port_url())
    try:
        elapsed, _exc = await _elapsed_raise(provider.get("k"))
    finally:
        await provider.aclose()
    assert elapsed < _BOUND_SECONDS


# --------------------------------------------------------------------------- #
# ... so every caller's fail-open / fail-closed branch actually runs
# --------------------------------------------------------------------------- #
class _Req:
    headers: dict[str, str] = {}
    scope: dict[str, Any] = {}

    class url:
        path = "/limited"

    class client:
        host = "203.0.113.9"


@pytest.mark.parametrize("fail_closed", [False, True])
async def test_rate_limiter_branches_on_hung_redis(
    hung_redis_url: str, monkeypatch: pytest.MonkeyPatch, fail_closed: bool
) -> None:
    from sandloom.core.ingress import rate_limit as rl

    provider = _provider(hung_redis_url)
    monkeypatch.setattr(rl, "cache", lambda name="default": provider)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    dep = rl.rate_limiter(requests=5, period=60, fail_closed=fail_closed)
    start = time.monotonic()
    try:
        if fail_closed:
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(dep(_Req()), _HANG_GUARD_SECONDS)  # type: ignore[arg-type]
            assert exc.value.status_code == 503
        else:
            assert await asyncio.wait_for(dep(_Req()), _HANG_GUARD_SECONDS) is None  # type: ignore[arg-type]
    finally:
        await provider.aclose()
    assert time.monotonic() - start < _BOUND_SECONDS


@pytest.mark.parametrize("fail_closed", [False, True])
async def test_revocation_branches_on_hung_redis(
    hung_redis_url: str, monkeypatch: pytest.MonkeyPatch, fail_closed: bool
) -> None:
    from sandloom.core.auth import marks, verification
    from sandloom.core.infra import access

    provider = _provider(hung_redis_url)
    monkeypatch.setattr(access, "cache", lambda name="default": provider)
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", fail_closed)
    claims = {"sub": str(uuid4()), "iat": int(time.time())}
    start = time.monotonic()
    try:
        if fail_closed:
            with pytest.raises(marks.RevocationUnavailableError):
                await asyncio.wait_for(
                    verification.is_token_revoked(claims), _HANG_GUARD_SECONDS
                )
        else:
            assert (
                await asyncio.wait_for(
                    verification.is_token_revoked(claims), _HANG_GUARD_SECONDS
                )
                is False
            )
    finally:
        await provider.aclose()
    assert time.monotonic() - start < _BOUND_SECONDS


@pytest.mark.parametrize("fail_closed", [False, True])
async def test_idempotency_branches_on_hung_redis(
    hung_redis_url: str, monkeypatch: pytest.MonkeyPatch, fail_closed: bool
) -> None:
    import httpx

    from sandloom.core import idempotency
    from sandloom.core.idempotency import cache_store as idempotency_cache_store

    provider = _provider(hung_redis_url)
    monkeypatch.setattr(
        idempotency_cache_store, "cache", lambda name="default": provider
    )
    app = FastAPI()

    @app.post(
        "/charge",
        dependencies=[
            Depends(idempotency.idempotency_guard(scope="t", fail_closed=fail_closed))
        ],
    )
    async def _charge() -> dict[str, bool]:
        return {"ok": True}

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            resp = await asyncio.wait_for(
                http.post("/charge", headers={"Idempotency-Key": "k1"}, json={}),
                _HANG_GUARD_SECONDS,
            )
    finally:
        await provider.aclose()
    assert resp.status_code == (503 if fail_closed else 200)
    assert time.monotonic() - start < _BOUND_SECONDS * 2  # reserve + release


@pytest.mark.parametrize("prod", [False, True])
async def test_lockout_branches_on_hung_redis(
    hung_redis_url: str, monkeypatch: pytest.MonkeyPatch, prod: bool
) -> None:
    from sandloom_identity import lockout

    provider = _provider(hung_redis_url)
    monkeypatch.setattr(lockout, "cache", lambda name="default": provider)
    start = time.monotonic()
    try:
        with patch.object(type(settings), "is_prod", new=property(lambda _s: prod)):
            if prod:
                with pytest.raises(HTTPException) as exc:
                    await asyncio.wait_for(
                        lockout.ensure_not_locked("email:x", client_ip="1.2.3.4"),
                        _HANG_GUARD_SECONDS,
                    )
                assert exc.value.status_code == 503
            else:
                await asyncio.wait_for(
                    lockout.ensure_not_locked("email:x", client_ip="1.2.3.4"),
                    _HANG_GUARD_SECONDS,
                )
                assert (
                    await asyncio.wait_for(
                        lockout.register_failure("email:x", client_ip="1.2.3.4"),
                        _HANG_GUARD_SECONDS,
                    )
                    is False
                )
    finally:
        await provider.aclose()
    assert time.monotonic() - start < _BOUND_SECONDS * 2


# --------------------------------------------------------------------------- #
# Fused single-round-trip operations against the real test Redis
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def live() -> AsyncIterator[RedisCacheProvider]:
    from pathlib import Path

    from sandloom.core.testing.workers import worker_env_values

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    url = os.getenv("CACHE_URL") or worker_env_values(env_test).get("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not configured")
    provider = _provider(url)
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await provider.aclose()
        pytest.skip("test Redis not reachable")
    yield provider
    await provider.aclose()


async def test_incr_window_counts_ttl_and_previous(live: RedisCacheProvider) -> None:
    key, prev = f"t:{uuid4()}:cur", f"t:{uuid4()}:prev"
    assert await live.incr_window(key, prev, ttl_seconds=30) == (1, None)
    await live.set(prev, "7", ttl_seconds=30)
    assert await live.incr_window(key, prev, ttl_seconds=30) == (2, "7")
    client = await live.raw_client()
    assert 0 < await client.ttl(key) <= 30


async def test_incr_many_counts_and_ttls(live: RedisCacheProvider) -> None:
    a, b = f"t:{uuid4()}:a", f"t:{uuid4()}:b"
    assert await live.incr_many([a, b], ttl_seconds=30) == [1, 1]
    assert await live.incr_many([a, b], ttl_seconds=30) == [2, 2]
    assert await live.incr_many([], ttl_seconds=30) == []
    client = await live.raw_client()
    assert 0 < await client.ttl(a) <= 30 and 0 < await client.ttl(b) <= 30


async def test_set_nx_get_creates_or_returns_existing(live: RedisCacheProvider) -> None:
    key = f"t:{uuid4()}"
    assert await live.set_nx_get(key, "first", ttl_seconds=30) == (True, None)
    assert await live.set_nx_get(key, "second", ttl_seconds=30) == (False, "first")
    assert await live.get(key) == "first"
    client = await live.raw_client()
    assert 0 < await client.ttl(key) <= 30


async def test_script_reloads_after_noscript(live: RedisCacheProvider) -> None:
    """EVALSHA falls back to SCRIPT LOAD + EVALSHA on NOSCRIPT."""
    from redis.exceptions import NoScriptError

    client = await live.raw_client()
    real = client.evalsha
    calls: list[str] = []

    async def _flaky(*args: Any) -> Any:
        calls.append("evalsha")
        if len(calls) == 1:
            raise NoScriptError("NOSCRIPT No matching script")
        return await real(*args)

    with patch.object(client, "evalsha", _flaky):
        assert await live.incr(f"t:{uuid4()}", ttl_seconds=30) == 1
    assert calls == ["evalsha", "evalsha"]


async def test_set_nx_get_degrades_on_old_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis < 7 rejects ``SET NX GET`` (syntax error): fall back to SET NX + GET."""
    from redis.exceptions import ResponseError

    store: dict[str, str] = {}

    class _OldRedis:
        async def set(self, key: str, value: str, **kw: Any) -> Any:
            if kw.get("get"):
                raise ResponseError("ERR syntax error")
            if kw.get("nx") and key in store:
                return None
            store[key] = value
            return True

        async def get(self, key: str) -> str | None:
            return store.get(key)

    provider = _provider("redis://unused:1/0")
    monkeypatch.setattr(RedisCacheProvider, "_nx_get_unsupported", False)

    async def _client() -> Any:
        return _OldRedis()

    monkeypatch.setattr(provider, "_client_obj", _client)
    assert await provider.set_nx_get("k", "a", ttl_seconds=5) == (True, None)
    assert RedisCacheProvider._nx_get_unsupported is True
    assert await provider.set_nx_get("k", "b", ttl_seconds=5) == (False, "a")


async def test_concurrent_transport_rebuild_after_loop_change_builds_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLM review #6: after a loop change two concurrent callers both found
    the stale transport pair; each closed it (an await) and then built its own
    sibling — the loser was overwritten and never closed (socket-pool leak)."""
    import asyncio

    built: list[Any] = []

    class _Client:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            await asyncio.sleep(0)  # a real close yields to other tasks
            self.closed = True

    def _build(*_args: Any, **_kwargs: Any) -> Any:
        client = _Client()
        built.append(client)
        return client

    old_main, old_transport = _Client(), _Client()
    monkeypatch.setattr(redis_cache, "_build_client", _build)
    monkeypatch.setattr(redis_cache, "build_transport_client", _build)
    monkeypatch.setattr(redis_cache, "_default_client", old_main)
    monkeypatch.setattr(
        redis_cache, "_default_client_origin", (old_main, object())
    )  # built on another (gone) loop
    monkeypatch.setattr(
        redis_cache, "_default_transport", {None: (old_main, old_transport)}
    )
    first, second = await asyncio.gather(
        redis_cache.get_default_transport_client(),
        redis_cache.get_default_transport_client(),
    )
    assert first is second
    assert old_main.closed and old_transport.closed
    live = {id(redis_cache._default_client), id(first)}
    leaked = [c for c in built if id(c) not in live and not c.closed]
    assert leaked == []
    assert len(built) == 2  # one main + one transport
