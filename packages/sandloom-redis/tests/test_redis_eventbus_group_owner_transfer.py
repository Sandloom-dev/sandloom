"""``extract-module`` moves the consumer group-owner fingerprint on real Redis.

``transfer_group_owners`` (the kernel's extraction step) over the Redis Streams
transport: the owner is listed, compare-and-set transferred to the target
database identity, and a stale owner is then refused. The memory-transport twin
stays in ``tests/integration/with_batteries/test_module_extraction.py``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from _redis_required import redis_unavailable
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport

from sandloom.core.cli.extract import transfer_group_owners
from sandloom.core.infra.ports.eventbus import GroupOwnedElsewhere, GroupOwner
from sandloom.core.testing import worker_env_values

REPO = Path(__file__).resolve().parents[3]


async def test_group_owner_transfer_redis_transport() -> None:
    values = worker_env_values(REPO / ".env.test")
    url = os.environ.get("SANDLOOM_TEST_CACHE_URL") or values.get("CACHE_URL")
    if not url:
        redis_unavailable("no CACHE_URL")
    client = redis_asyncio.from_url(url)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
        await client.aclose()
        redis_unavailable("Redis unavailable")
    prefix = f"tx{uuid.uuid4().hex[:8]}:"

    async def _factory() -> Any:
        return client

    t = RedisStreamsTransport(client_factory=_factory, prefix=prefix)
    try:
        src_id, dst_id = "url:aaaa", "url:bbbb"
        mine = GroupOwner("sandloom_billing", src_id)
        await t.claim_group_owner("billing.x.v1", "sandloom_billing:n@c", mine)
        listed = await t.list_group_owners()
        assert listed == [("billing.x.v1", "sandloom_billing:n@c", mine)]
        moved = await transfer_group_owners(
            t,
            "sandloom_billing",
            source_identity=src_id,
            target_identity=dst_id,
            apply=True,
        )
        assert moved == ["billing.x.v1 sandloom_billing:n@c"]
        await t.claim_group_owner(
            "billing.x.v1", "sandloom_billing:n@c", replace(mine, database=dst_id)
        )
        with pytest.raises(GroupOwnedElsewhere):
            await t.claim_group_owner("billing.x.v1", "sandloom_billing:n@c", mine)
        assert not await t.transfer_group_owner(
            "billing.x.v1", "sandloom_billing:n@c", expected=mine, owner=mine
        )  # compare-and-set: the owner is no longer ``mine``
    finally:
        keys = [k async for k in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
