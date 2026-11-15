"""The ``CacheConformance`` kit against the ``redis`` cache driver (a real Redis,
``settings.cache_url``), and what the kit cannot know: the scripting probe of
``health`` and the server identity.

The kit's own bite on deliberately broken drivers stays in the kernel
(``sandloom/core/tests/test_cache_kit.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any
from uuid import uuid4

import pytest
import redis.exceptions as redis_errors
from sandloom_redis.cache import RedisCacheProvider

from sandloom.core.config.settings import settings
from sandloom.driverkit import CacheProvider, InvalidArgument
from sandloom.driverkit.testing import CacheConformance


def _redis(**options: Any) -> RedisCacheProvider:
    options.setdefault("url", settings.cache_url)
    return RedisCacheProvider(name="kit", driver="redis", options=options, shared=False)


class TestRedisCache(CacheConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], CacheProvider]:
        # The kit's races open 50 connections at once: the production defaults
        # (0.5 s socket and connect timeouts) time out on a loaded CI runner.
        return partial(
            _redis,
            socket_timeout_seconds=5.0,
            socket_connect_timeout_seconds=5.0,
            pool_timeout_seconds=5.0,
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], CacheProvider]:
        # Nothing listens on port 1; the "password" is the kit's secret.
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: _redis(url=url, retry_attempts=0)

    # The driver declares ``cas``, so the check of a driver without it is
    # skipped on every lane; a ``None`` attribute is not collected.
    test_without_cas_both_calls_raise_unsupported = None  # type: ignore[assignment]


def test_redis_declares_cas_and_its_hints() -> None:
    provider = _redis()
    assert provider.contract_version == 2
    assert provider.tier == "beta"
    assert provider.features() == {"cas"}
    assert provider.development_only is False
    assert set(provider.performance_hints) == {
        "get_many",
        "set_many",
        "windowed_counter",
    }


async def test_the_real_server_identity_is_its_run_id() -> None:
    provider = _redis()
    await provider.aopen()
    try:
        info = await (await provider.redis_client()).info(section="server")
        assert await provider.isolation_identity() == f"redis:{info['run_id']}"
    finally:
        await provider.aclose()


async def test_a_key_of_another_redis_type_is_invalid_not_unavailable() -> None:
    """A WRONGTYPE reply is the caller's key clash, not a retryable outage."""
    provider = _redis()
    await provider.aopen()
    try:
        key = f"kit:wrongtype:{uuid4().hex}"
        client = await provider.redis_client()
        await client.rpush(key, "a")
        try:
            for call in (
                partial(provider.get, key),
                partial(provider.incr, key, ttl_seconds=30),
            ):
                with pytest.raises(InvalidArgument):
                    await call()
        finally:
            await client.delete(key)
    finally:
        await provider.aclose()


class _Client:
    """A scripted client: ``ping`` answers, ``evalsha`` answers or raises."""

    def __init__(self, scripting: Exception | None) -> None:
        self._scripting = scripting
        self.script_calls = 0

    async def ping(self) -> bool:
        return True

    async def evalsha(self, sha: str, numkeys: int, *args: Any) -> int:
        self.script_calls += 1
        if self._scripting is not None:
            raise self._scripting
        return 1


def _provider_with(client: _Client) -> RedisCacheProvider:
    provider = _redis()

    async def _obj() -> Any:
        return client

    provider._client_obj = _obj  # type: ignore[method-assign]  # a scripted client
    return provider


async def test_health_is_ok_when_ping_and_scripting_work() -> None:
    client = _Client(scripting=None)
    health = await _provider_with(client).health()
    assert health.status == "ok"
    assert client.script_calls == 1


@pytest.mark.parametrize(
    "refusal",
    [
        redis_errors.NoPermissionError(
            "this user has no permissions to run the 'script' command"
        ),
        redis_errors.ResponseError("unknown command 'EVALSHA' (proxy)"),
    ],
)
async def test_health_fails_when_the_server_refuses_scripting(
    refusal: Exception,
) -> None:
    """``incr``, ``windowed_counter`` and ``cas`` run as scripts: a server that
    refuses them must not report ok (rate limits would fail open)."""
    health = await _provider_with(_Client(scripting=refusal)).health()
    assert health.status == "fail"
    assert health.details == {"error": type(refusal).__name__}
    assert "proxy" not in str(health.details)  # the type only, never the text


async def test_health_against_a_real_server_checks_scripting() -> None:
    provider = _redis()
    await provider.aopen()
    try:
        assert (await provider.health()).status == "ok"
    finally:
        await provider.aclose()
