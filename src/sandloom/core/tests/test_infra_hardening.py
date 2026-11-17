"""Regression tests for the infrastructure-layer review hardening.

Covers: event-bus publish preflight (so invalid/oversized events can't be
committed to the outbox and then fail at dispatch), bounded shutdown drain,
and the key-based ObjectStore contract (including filesystem
traversal/symlink-escape rejection). The operation-based Redis cache provider is
tested in ``packages/sandloom-redis/tests/test_redis_cache_operations.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.contracts.validators import ContractValidationError
from sandloom.core.drivers.filesystem_storage import Storage
from sandloom.core.drivers.local_objectstore import LocalObjectStoreProvider
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import BroadcastEventBus
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
    bus = BroadcastEventBus(InMemorySchemaRegistry(), broadcast=provider)

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
