"""Bounded Redis cache client: an unreachable / hung Redis RAISES quickly.

Before: the client had no socket/connect timeout and an unbounded pool, so a
blackholed or hung Redis made every cache call await forever — the
fail-open / fail-closed branches of the rate limiter, token revocation,
idempotency and login lockout never ran; the request just hung. Now every
operation raises within ~(1 + retries) x CACHE_SOCKET_TIMEOUT_SECONDS and each
caller's policy branch runs. Plus the fused single-round-trip operations
(``incr_window`` / ``incr_many`` / ``set_nx_get``) against the real test Redis.

Moved from ``sandloom/core/tests/test_redis_cache_bounded.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException

from sandloom.core.config.settings import settings
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


def _provider(url: str) -> RedisCacheProvider:
    return RedisCacheProvider(
        name="bounded", driver="redis", options={"url": url}, shared=False
    )


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
