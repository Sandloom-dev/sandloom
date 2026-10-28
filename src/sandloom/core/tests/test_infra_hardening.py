"""Regression tests for the infrastructure-layer review hardening.

Covers: event-bus publish preflight (so invalid/oversized events can't be
committed to the outbox and then fail at dispatch), bounded shutdown drain,
the operation-based CacheProvider, and the key-based ObjectStore contract
(including filesystem traversal/symlink-escape rejection).
"""

from __future__ import annotations

import asyncio

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.contracts.validators import ContractValidationError
from sandloom.core.drivers.filesystem_storage import Storage, UnsafeStoragePathError
from sandloom.core.drivers.local_objectstore import LocalObjectStoreProvider
from sandloom.core.drivers.redis_cache import create_redis_cache
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import RedisEventBus
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
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


class _FakePubSub:
    def __init__(self) -> None:
        self.subscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def psubscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def close(self) -> None:
        return None

    async def listen(self):  # async generator
        while True:
            await asyncio.sleep(3600)
            yield {}


class _FakeRedis:
    def __init__(self) -> None:
        self._pubsub = _FakePubSub()

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


@pytest.mark.asyncio
async def test_concurrent_subscribe_starts_one_listen_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRedis()

    async def _get_cache():
        return fake

    # The driver injects the client factory; construct the bus the same way.
    bus = RedisEventBus(InMemorySchemaRegistry(), client_factory=_get_cache)

    async def _noop(_msg) -> None:
        return None

    # Race many first-subscribers: the task must be created exactly once.
    await asyncio.gather(*(bus.subscribe(f"core.c{i}.v1", _noop) for i in range(8)))

    def _is_listen_loop(task: asyncio.Task) -> bool:
        coro = task.get_coro()
        return getattr(coro, "__qualname__", "") == "RedisEventBus._listen_loop"

    listen_loops = [t for t in asyncio.all_tasks() if _is_listen_loop(t)]
    assert len(listen_loops) == 1
    await bus.close()


# --- #4 operation-based cache --------------------------------------------------


class _OpsRedis:
    """Minimal Redis surface the cache provider's ops delegate to."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key, value, nx: bool = False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def incr(self, key: str) -> int:
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key: str, ttl: int) -> None:
        return None


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
    # incr() falls back to INCR (+EXPIRE) when no eval/Lua support.
    assert await provider.incr("ctr", ttl_seconds=60) == 1
    assert await provider.incr("ctr", ttl_seconds=60) == 2
    await provider.delete("k")
    assert await provider.get("k") is None


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
    from sandloom.core.infra.registry import FeatureUnsupportedError

    assert "url" not in store.features()
    with pytest.raises(FeatureUnsupportedError, match="no HTTP retrieval route"):
        await store.url("a/b.txt")
    await store.delete("a/b.txt")
    assert not (tmp_path / "a" / "b.txt").exists()


@pytest.mark.asyncio
async def test_objectstore_rejects_traversal(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(UnsafeStoragePathError):
        await store.put("../escape.txt", b"x")


@pytest.mark.asyncio
async def test_objectstore_rejects_symlink_escaping_root(tmp_path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside, target_is_directory=True)
    store = _store(root)
    with pytest.raises(UnsafeStoragePathError):
        await store.put("evil/x.txt", b"data")
    assert not (outside / "x.txt").exists()
