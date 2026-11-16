"""The operation-based ``redis`` cache provider (infrastructure-layer review
hardening #4): its operations delegate to the client, and an atomic operation
has no non-atomic fallback on a client without scripting. The rest of the
review's regression tests (publish preflight, bounded drain, object store)
stay in ``src/sandloom/core/tests/test_infra_hardening.py``.
"""

from __future__ import annotations

import pytest
from redis.exceptions import NoScriptError
from sandloom_redis.cache import (
    RedisCacheProvider,
    _script_sha,
    create_redis_cache,
)

from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig

# --- #4 operation-based cache --------------------------------------------------


class _PlainRedis:
    """Plain commands only: a client without scripting."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key: str, ttl: int) -> None:
        raise AssertionError("a non-atomic EXPIRE must never be sent")

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key, value, nx: bool = False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class _OpsRedis(_PlainRedis):
    """The surface the cache provider's ops delegate to: plain commands plus
    the scripting (``EVALSHA`` / ``SCRIPT LOAD``) its atomic operations use."""

    def __init__(self) -> None:
        super().__init__()
        self.loaded: set[str] = set()
        self.expires: dict[str, int] = {}

    async def script_load(self, script: str) -> str:
        sha = _script_sha(script)
        self.loaded.add(sha)
        return sha

    async def evalsha(self, sha: str, numkeys: int, *keys_args: str | int) -> int:
        """The atomic INCR+EXPIRE script the driver sends, run in Python.

        Like a real server: ``NOSCRIPT`` until the script was loaded; a script
        this double does not implement fails loudly.
        """
        if sha not in self.loaded:
            raise NoScriptError("no matching script")
        assert sha == RedisCacheProvider._INCR_WITH_TTL_SHA, "unexpected script"
        key, ttl = str(keys_args[0]), int(keys_args[numkeys])
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        if self.store[key] == "1" and ttl > 0:
            self.expires[key] = ttl
        return int(self.store[key])


@pytest.mark.asyncio
async def test_cache_provider_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OpsRedis()

    async def _get_cache():
        return fake

    monkeypatch.setattr("sandloom_redis.cache.get_default_client", _get_cache)
    provider = create_redis_cache(
        InstanceConfig(
            capability="cache", name=DEFAULT_INSTANCE, driver="redis", options={}
        )
    )

    await provider.set("k", "v")
    assert await provider.get("k") == "v"
    # add() is reserve-or-conflict.
    assert await provider.add("once", "1", ttl_seconds=60) is True
    assert await provider.add("once", "1", ttl_seconds=60) is False
    # incr() is one atomic script (INCR + EXPIRE on the first hit), loaded on NOSCRIPT.
    assert await provider.incr("ctr", ttl_seconds=60) == 1
    assert await provider.incr("ctr", ttl_seconds=60) == 2
    assert fake.expires == {"ctr": 60}
    await provider.delete("k")
    assert await provider.get("k") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        lambda p: p.incr("k", ttl_seconds=60),
        lambda p: p.windowed_counter("k", "k-prev", ttl_seconds=60),
        lambda p: p.compare_and_set("k", "a", "b"),
        lambda p: p.compare_and_delete("k", "a"),
    ],
    ids=["incr", "windowed_counter", "compare_and_set", "compare_and_delete"],
)
async def test_cache_atomic_operations_have_no_non_atomic_fallback(
    monkeypatch: pytest.MonkeyPatch, operation
) -> None:
    """A client without scripting fails; it is never served by plain commands."""
    fake = _PlainRedis()

    async def _get_cache():
        return fake

    monkeypatch.setattr("sandloom_redis.cache.get_default_client", _get_cache)
    provider = create_redis_cache(
        InstanceConfig(
            capability="cache", name=DEFAULT_INSTANCE, driver="redis", options={}
        )
    )
    with pytest.raises(AttributeError, match="evalsha"):
        await operation(provider)
    assert fake.store == {}
