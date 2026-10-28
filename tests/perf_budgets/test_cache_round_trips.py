"""Redis round-trip budgets of the framework's per-request cache primitives.

Measured on a probe app wired with the framework's own guard / rate limiter /
idempotency guard + capture middleware, against the real test Redis, so the
numbers are exactly what a route using them pays.

Before -> after (this change):
* authenticated request (revocation check + emergency block): 1 round trip
  (ADR-0009: one GET of the subject-state record, which holds both)
* + rate limiter:                            +2 -> +1 (INCR/EXPIRE + GET
  previous window fused into one EVALSHA)
* + idempotent FIRST request (reserve + finalise): 3 -> 2 (GET + SET NX
  fused into SET NX GET)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from sandloom.core import idempotency
from sandloom.core.auth import tokens
from sandloom.core.config.settings import settings
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import capture as idempotency_capture
from sandloom.core.idempotency import replay as idempotency_replay
from sandloom.core.infra import access
from sandloom.core.ingress import rate_limit as rl

from ._counters import RedisCommandCounter

BUDGET_AUTH_GET = 1
BUDGET_RATE_LIMIT_EXTRA = 1
BUDGET_IDEMPOTENT_FIRST = 2  # + the auth check


def _cache_url() -> str:
    import os

    from sandloom.core.testing.workers import worker_env_values

    env = worker_env_values(Path(__file__).resolve().parents[2] / ".env.test")
    url = os.getenv("CACHE_URL") or env.get("CACHE_URL")
    if not url:
        pytest.skip("no test CACHE_URL configured")
    return str(url)


@pytest_asyncio.fixture
async def counted_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[RedisCacheProvider, RedisCommandCounter]]:
    provider = RedisCacheProvider(
        name="budget", driver="redis", options={"url": _cache_url()}, shared=False
    )
    client = await provider.raw_client()
    counter = RedisCommandCounter()
    counter.wrap(client, monkeypatch)
    for module in (access, rl, idempotency_cache_store):
        monkeypatch.setattr(module, "cache", lambda name="default": provider)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    # Warm the Lua script cache (a NOSCRIPT reload is a one-off per server).
    await provider.incr_window(f"warm:{uuid4()}", "warm:prev", ttl_seconds=5)
    await provider.compare_and_set(f"warm:{uuid4()}", "a", "b", ttl_seconds=5)
    yield provider, counter
    await provider.aclose()


def _probe_app() -> FastAPI:
    from sandloom.shared.guards.auth import get_current_user_id

    app = FastAPI()
    app.add_middleware(idempotency.IdempotencyCaptureMiddleware)
    app.add_exception_handler(
        idempotency_replay._ReplayResponse, idempotency_replay._replay_handler
    )

    @app.get("/auth", dependencies=[Depends(get_current_user_id)])
    async def _auth() -> dict[str, bool]:
        return {"ok": True}

    @app.get(
        "/limited",
        dependencies=[
            Depends(get_current_user_id),
            Depends(rl.rate_limiter(requests=1000, period=60)),
        ],
    )
    async def _limited() -> dict[str, bool]:
        return {"ok": True}

    @app.post(
        "/idem",
        dependencies=[
            Depends(get_current_user_id),
            Depends(idempotency.idempotency_guard(scope="budget")),
        ],
    )
    async def _idem() -> dict[str, bool]:
        return {"ok": True}

    return app


def _headers() -> dict[str, str]:
    token = tokens.create_access_token(
        data={"sub": str(uuid4()), "token_type": "access"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _send(method: str, path: str, **kw: Any) -> int:
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_probe_app()), base_url="http://b"
    ) as http:
        resp = await http.request(method, path, **kw)
    return resp.status_code


async def test_authenticated_get_budget(counted_cache: Any) -> None:
    _provider, counter = counted_cache
    headers = _headers()
    counter.enabled = True
    assert await _send("GET", "/auth", headers=headers) == 200
    assert counter.count <= BUDGET_AUTH_GET, counter.commands
    assert counter.commands == ["GET"]  # the one subject-state record


async def test_rate_limited_request_budget(counted_cache: Any) -> None:
    _provider, counter = counted_cache
    headers = _headers()
    counter.enabled = True
    assert await _send("GET", "/limited", headers=headers) == 200
    assert counter.count <= BUDGET_AUTH_GET + BUDGET_RATE_LIMIT_EXTRA, counter.commands
    assert counter.commands.count("EVALSHA") == 1


async def test_idempotent_first_request_budget(counted_cache: Any) -> None:
    _provider, counter = counted_cache
    headers = {**_headers(), "Idempotency-Key": f"k-{time.time_ns()}"}
    counter.enabled = True
    assert await _send("POST", "/idem", headers=headers, json={"a": 1}) == 200
    # The terminal record may be written after the response (detached task):
    # let it land before counting.
    await asyncio.gather(*list(idempotency_capture._background_persists))
    assert counter.count <= BUDGET_AUTH_GET + BUDGET_IDEMPOTENT_FIRST, counter.commands
    # Replay of the same key: auth + one SET NX GET.
    counter.reset()
    assert await _send("POST", "/idem", headers=headers, json={"a": 1}) == 200
    assert counter.count <= BUDGET_AUTH_GET + 1, counter.commands
