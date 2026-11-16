"""ADR-0009 item 13 on the Redis ``atomic_store``: the fused subject-state
operation is one atomic Lua script with the same order as the in-memory store.

The record's value domain, the block/revocation/unblock rules and the hot path
(one ``GET``) are pinned against the in-memory store in
``src/sandloom/core/tests/test_subject_record.py``.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from _redis_required import cache_url_or_unavailable, redis_unavailable
from sandloom_redis.atomic_store import RedisAtomicStoreProvider

from sandloom.core.auth import blocks, marks
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.tests.test_subject_record import _after, _blocked


@pytest_asyncio.fixture
async def live() -> AsyncIterator[RedisAtomicStoreProvider]:
    url = cache_url_or_unavailable()
    provider = RedisAtomicStoreProvider(
        name="subject",
        driver="redis",
        # These tests prove atomic ordering, not the driver's production
        # bounds (0.5 s socket, 1 s pool wait): a loaded shared runner stalls
        # the loop past those and fails a correct script with ``Timeout``.
        options={
            "url": url,
            "socket_timeout_seconds": 30,
            "socket_connect_timeout_seconds": 30,
            "pool_timeout_seconds": 30,
        },
    )
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
        await provider.aclose()
        redis_unavailable("test Redis not reachable")
    yield provider
    await provider.aclose()


async def test_redis_subject_state_order_and_ttl(
    live: RedisAtomicStoreProvider,
) -> None:
    assert live.performance_hints["set_subject_state"] == "one_round_trip"
    key = f"t:subject:{uuid.uuid4()}"
    client = await live._client_obj()
    assert await live.set_subject_state(key, _after(100.5), ttl_seconds=30) == (
        _after(100.5)
    )
    assert 0 < await client.ttl(key) <= 30
    assert await live.set_subject_state(key, _after(100.4), ttl_seconds=5) == (
        _after(100.5)
    )
    # a block outranks and clears the TTL (no expiry)
    assert await live.set_subject_state(key, _blocked(1)) == _blocked(1)
    assert await client.ttl(key) == -1
    # no revocation, however late, unblocks
    assert await live.set_subject_state(
        key, _after(9_999_999_999), ttl_seconds=30
    ) == _blocked(1)
    # a corrupt value is replaced
    await live.set(key, "1700000000")
    assert await live.set_subject_state(key, _after(0)) == _after(0)
    with pytest.raises(InvalidArgument):
        await live.set_subject_state(key, "12")
    await live.delete(key)


async def test_redis_concurrent_block_and_revocations_end_blocked(
    live: RedisAtomicStoreProvider,
) -> None:
    key = f"t:subject:{uuid.uuid4()}"
    values = [_after(1_700_000_000 + i) for i in range(200)] + [_blocked(5)]
    random.Random(3).shuffle(values)

    async def _stall() -> None:
        # A loaded runner in miniature: block the loop while the 201 commands
        # are in flight, longer than the driver's default 0.5 s socket timeout
        # (the fixture's wider bounds must absorb it; the defaults fail here).
        await asyncio.sleep(0.002)
        time.sleep(0.8)

    await asyncio.gather(
        _stall(), *(live.set_subject_state(key, v, ttl_seconds=30) for v in values)
    )
    assert await live.get(key) == _blocked(5)
    await live.delete(key)


async def test_redis_unblock_swaps_and_never_deletes(
    live: RedisAtomicStoreProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = str(uuid.uuid4())
    key = marks.subject_key(sub)
    await live.set_subject_state(key, _blocked(time.time()))
    assert await blocks._swap_blocked_for_after(live, sub) is True
    raw = await live.get(key)
    assert raw is not None and not marks.state_is_blocked(raw)
    assert 0 < await (await live._client_obj()).ttl(key)
    # nothing to lift any more
    assert await blocks._swap_blocked_for_after(live, sub) is False
    await live.delete(key)
