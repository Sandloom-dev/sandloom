"""The cache client never re-sends a non-idempotent command on a lost reply.

Code review 2026-10-21 (Infra #6): redis-py applies a connection's ``Retry``
to a whole command — send AND reply — when ``retry_on_error`` is set; a
connection dropped after Redis executed an ``INCR`` / Lua script (CAS,
``incr_window``, ``incr_many``, ``set_max``) or ``SET NX`` would then re-run
it (a counter moves twice; a CAS that already swapped returns 0 and
``idempotency_guard`` believes it lost ownership). With the pinned redis-py
5.2.1 and no ``retry_on_error`` a command is sent once, so these tests pass
on the stock client. They pin that behaviour: if a redis-py upgrade (or a
client option) starts re-sending commands, they fail — implement an
explicit idempotency-aware policy then. (A subclass re-implementing
``execute_command`` was tried and reverted: it copied private redis-py
internals and bypassed the transport fault seams other suites rely on.)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.testing.workers import worker_env_values


@pytest_asyncio.fixture
async def live() -> AsyncIterator[RedisCacheProvider]:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    url = (
        os.getenv("SANDLOOM_TEST_CACHE_URL")
        or os.getenv("CACHE_URL")
        or worker_env_values(env_test).get("CACHE_URL")
    )
    if not url:
        pytest.skip("CACHE_URL not configured")
    provider = RedisCacheProvider(
        name="retry-policy", driver="redis", options={"url": url}, shared=False
    )
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await provider.aclose()
        pytest.skip("test Redis not reachable")
    yield provider
    await provider.aclose()


class _LoseReplyOnce:
    """Run the command, then drop its reply ONCE (connection reset)."""

    def __init__(self, client: Any, command: str, *, execute: bool = True) -> None:
        self.inner = client._send_command_parse_response
        self.command = command
        self.execute = execute
        self.calls = 0
        self.armed = True

    async def __call__(
        self, conn: Any, command_name: Any, *args: Any, **kw: Any
    ) -> Any:
        if str(command_name).upper() == self.command:
            self.calls += 1
            if self.armed:
                self.armed = False
                if self.execute:
                    await self.inner(conn, command_name, *args, **kw)
                raise RedisConnectionError("Connection reset by peer")
        return await self.inner(conn, command_name, *args, **kw)


async def _arm(provider: RedisCacheProvider, command: str, **kw: Any) -> Any:
    client = await provider._client_obj()
    fault = _LoseReplyOnce(client, command, **kw)
    client._send_command_parse_response = fault
    return fault


async def test_cas_whose_reply_was_lost_is_not_resent(live: RedisCacheProvider) -> None:
    key = f"sandloom:test:cas:{uuid4()}"
    await live.set(key, "owner-1:pending")
    await live.compare_and_set(key, "owner-1:pending", "warmup", ttl_seconds=60)
    await live.set(key, "owner-1:pending")  # script loaded, value reset
    fault = await _arm(live, "EVALSHA")
    # Before: the retry re-ran the CAS, saw its own new value and returned
    # False ("lost ownership") although the swap happened.
    with pytest.raises(RedisConnectionError):
        await live.compare_and_set(key, "owner-1:pending", "owner-1:done")
    assert fault.calls == 1
    assert await live.get(key) == "owner-1:done"


async def test_incr_whose_reply_was_lost_counts_once(live: RedisCacheProvider) -> None:
    key = f"sandloom:test:incr:{uuid4()}"
    assert await live.incr(key, ttl_seconds=60) == 1  # script loaded
    fault = await _arm(live, "EVALSHA")
    with pytest.raises(RedisConnectionError):
        await live.incr(key, ttl_seconds=60)
    assert fault.calls == 1
    assert await live.get(key) == "2"  # not 3


async def test_set_nx_whose_reply_was_lost_is_not_resent(
    live: RedisCacheProvider,
) -> None:
    key = f"sandloom:test:add:{uuid4()}"
    fault = await _arm(live, "SET")
    with pytest.raises(RedisConnectionError):
        await live.add(key, "v", ttl_seconds=60)
    assert fault.calls == 1
    assert await live.get(key) == "v"
