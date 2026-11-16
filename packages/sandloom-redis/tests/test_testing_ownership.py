"""Hard ownership of a test Redis database (``sandloom_redis.testing.ownership``).

A run never flushes a Redis index another run marked as its own. The rules and the
refusal are the kernel's (``check_holder``, tested in
``src/sandloom/core/tests/test_testing_ownership.py``); these tests run them
against a real Redis. Guarantee G-OPS-07.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from _redis_required import cache_url_or_unavailable, redis_unavailable
from sandloom_redis.testing import flush_owned_redis
from sandloom_redis.testing.ownership import REDIS_OWNER_KEY, claim_redis

from sandloom.core.testing import ownership

BACKEND = Path(__file__).resolve().parents[3]


@pytest.fixture
async def own_redis() -> AsyncIterator[Any]:
    redis = pytest.importorskip("redis.asyncio")
    url = cache_url_or_unavailable()
    if not url.startswith("redis"):
        redis_unavailable("the test cache url is not a Redis one")
    client = redis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - test Redis unreachable: skip
        redis_unavailable(f"Redis unreachable: {exc}")
    previous = await client.get(REDIS_OWNER_KEY)
    try:
        yield client
    finally:
        if previous is None:
            await client.delete(REDIS_OWNER_KEY)
        else:
            await client.set(REDIS_OWNER_KEY, previous)
        await client.aclose()


async def test_flush_refuses_another_runs_redis_and_keeps_its_data(
    own_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = own_redis
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    await client.set(REDIS_OWNER_KEY, "run-a")
    await client.set("sandloom:test:probe", "kept")
    with pytest.raises(ownership.TestResourceOwnershipError, match="run-a"):
        await flush_owned_redis(client, owner="run-b")
    assert await client.get("sandloom:test:probe") == b"kept"
    await flush_owned_redis(client, owner="run-a")
    assert await client.get("sandloom:test:probe") is None
    # The marker survives its owner's flush.
    assert await client.get(REDIS_OWNER_KEY) == b"run-a"


async def test_takeover_flushes_and_re_stamps_another_runs_redis(
    own_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = own_redis
    await client.set(REDIS_OWNER_KEY, "run-a")
    monkeypatch.setenv(ownership.TAKEOVER_ENV, "1")
    with pytest.warns(UserWarning, match="taking over"):
        await flush_owned_redis(client, owner="run-b")
    assert await client.get(REDIS_OWNER_KEY) == b"run-b"


def test_claim_redis_refuses_another_owner(
    own_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    url = cache_url_or_unavailable()
    ownership._claimed.clear()
    claim_redis(url, owner=ownership.current_owner())
    with pytest.raises(
        ownership.TestResourceOwnershipError, match="SANDLOOM_TEST_CACHE"
    ):
        claim_redis(url, owner="run-b")
