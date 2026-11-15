"""Regression tests for the infrastructure-layer review hardening.

Covers: event-bus publish preflight (so invalid/oversized events can't be
committed to the outbox and then fail at dispatch), bounded shutdown drain,
the operation-based CacheProvider, and the key-based ObjectStore contract
(including filesystem traversal/symlink-escape rejection).
"""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import NoScriptError

from sandloom.core.config.settings import settings
from sandloom.core.contracts.validators import ContractValidationError
from sandloom.core.drivers.filesystem_storage import Storage
from sandloom.core.drivers.local_objectstore import LocalObjectStoreProvider
from sandloom.core.drivers.redis_cache import (
    RedisCacheProvider,
    _script_sha,
    create_redis_cache,
)
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import register_event_schemas


def _bus() -> InMemoryEventBus:
    return InMemoryEventBus(InMemorySchemaRegistry())


# --- #1 publish preflight ------------------------------------------------------


def test_preflight_rejects_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "event_payload_max_bytes", 8)
    bus = _bus()
    register_event_schemas(["core.test.v1"], target=bus)
    with pytest.raises(ValueError):
        bus.preflight("core.test.v1", {"data": "way too long to fit"})
    # A small payload is accepted.
    bus.preflight("core.test.v1", {"n": 1})


def test_preflight_rejects_missing_schema() -> None:
    bus = _bus()  # empty registry -> no schema for the versioned channel
    with pytest.raises(ContractValidationError):
        bus.preflight("core.unregistered.v1", {"n": 1})


# --- #3 bounded shutdown drain -------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_drain_cancels_hung_callback() -> None:
    bus = _bus()
    started = asyncio.Event()

    async def _hang() -> None:
        started.set()
        await asyncio.sleep(3600)

    bus._track_task(asyncio.create_task(_hang()))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # Must return promptly despite the never-ending callback.
    await asyncio.wait_for(bus._drain_bg_tasks(timeout=0.05), timeout=1.0)
    assert not bus._bg_tasks  # done-callback removed the cancelled task


# --- #2 subscribe starts a single listen loop ----------------------------------


@pytest.mark.asyncio
async def test_concurrent_subscribe_starts_one_listen_loop() -> None:
    from sandloom.core.drivers.memory_broadcast import MemoryBroadcastProvider

    provider = MemoryBroadcastProvider(
        name="default", driver="memory", options={"namespace": "hardening"}
    )
    bus = RedisEventBus(InMemorySchemaRegistry(), broadcast=provider)

    async def _noop(_msg) -> None:
        return None

    # Race many first-subscribers of one channel: its task is created once.
    await asyncio.gather(*(bus.subscribe("core.c.v1", _noop) for _ in range(8)))

    def _is_listen_loop(task: asyncio.Task) -> bool:
        return task.get_name() == "bus:core.c.v1"

    listen_loops = [t for t in asyncio.all_tasks() if _is_listen_loop(t)]
    assert len(listen_loops) == 1
    await bus.close()
    await provider.aclose()


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

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", _get_cache
    )
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

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", _get_cache
    )
    provider = create_redis_cache(
        InstanceConfig(
            capability="cache", name=DEFAULT_INSTANCE, driver="redis", options={}
        )
    )
    with pytest.raises(AttributeError, match="evalsha"):
        await operation(provider)
    assert fake.store == {}


# --- #6 key-based object store + traversal/symlink rejection -------------------


def _store(path) -> LocalObjectStoreProvider:
    return LocalObjectStoreProvider(
        name="t",
        driver="local",
        options={},
        storage=Storage(storage_type="local", path=str(path)),
    )


@pytest.mark.asyncio
async def test_objectstore_roundtrip(tmp_path) -> None:
    store = _store(tmp_path)
    loc = await store.put("a/b.txt", b"hello", content_type="text/plain")
    assert (tmp_path / "a" / "b.txt").read_bytes() == b"hello"
    assert loc == "a/b.txt"
    assert await store.get("a/b.txt") == b"hello"
    assert "presigned_get" not in store.features()
    with pytest.raises(Unsupported) as refused:
        await store.url("a/b.txt")
    assert refused.value.feature == "presigned_get"
    await store.delete("a/b.txt")
    assert not (tmp_path / "a" / "b.txt").exists()


@pytest.mark.asyncio
async def test_objectstore_rejects_traversal(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(InvalidArgument):
        await store.put("../escape.txt", b"x")


@pytest.mark.asyncio
async def test_objectstore_rejects_symlink_escaping_root(tmp_path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside, target_is_directory=True)
    store = _store(root)
    with pytest.raises(InvalidArgument):
        await store.put("evil/x.txt", b"data")
    assert not (outside / "x.txt").exists()
