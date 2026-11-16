"""A ``never_dead_letter`` group survives the old delivery budget on real Redis Streams.

The kernel's side (the bus and the consumer wrapper retrying a ``dead_letter=False``
consumer forever, ``/ready`` degrading while a revocation is stuck) runs on the memory
transport in ``src/sandloom/core/tests/test_revocation_never_dead_lettered.py``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import redis.asyncio as redis_asyncio
from _redis_required import cache_url_or_unavailable, redis_unavailable
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport

from sandloom.core.auth import marks
from sandloom.core.eventing.transport_base import DLQ_REASON_POISON
from sandloom.core.testing import reload_test_env

CHANNEL = marks.TOKENS_REVOKED_CHANNEL
#: The old delivery budget after which the event used to be dead-lettered.
OLD_BUDGET = 5


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "channel": CHANNEL,
        "schema_version": "v1",
        "trace_id": None,
        "emitted_at": "2026-01-01T00:00:00+00:00",
        "payload": payload,
    }


async def test_redis_streams_never_dead_letter_group_survives_the_old_budget() -> None:
    url = cache_url_or_unavailable()
    client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
        await client.aclose()
        redis_unavailable("Redis unreachable")
    stream = f"test.revocation.{uuid4().hex[:8]}.v1"

    async def _factory() -> Any:
        return client

    transport = RedisStreamsTransport(
        client_factory=_factory,
        max_deliveries=OLD_BUDGET,
        retry_base_ms=1,
        retry_max_ms=5,
    )
    group = "sandloom:auth@tokens_revoked@local"
    transport.never_dead_letter(stream, group)
    await transport.ensure_group(stream, group)
    try:
        await transport.publish(stream, _envelope({"n": 1}))  # type: ignore[arg-type]
        attempts = 0
        consumer = "c1"
        while attempts < OLD_BUDGET * 3:  # the handler keeps failing
            for _ in await transport.consume(stream, group=group, consumer=consumer):
                attempts += 1
            await asyncio.sleep(0.01)
        assert await transport.list_dlq(stream) == []
        delivered: list[Any] = []
        deadline = time.monotonic() + 5
        while not delivered and time.monotonic() < deadline:
            delivered = await transport.consume(stream, group=group, consumer=consumer)
            await asyncio.sleep(0.01)
        assert len(delivered) == 1  # still deliverable after 15 failures
        await transport.ack(delivered[0])
        assert await transport.list_dlq(stream) == []

        # ... and an explicit poison still reaches the DLQ.
        await transport.publish(stream, _envelope({"n": 2}))  # type: ignore[arg-type]
        (poison,) = await transport.consume(stream, group=group, consumer=consumer)
        await transport.dead_letter(poison, reason=DLQ_REASON_POISON)
        (parked,) = await transport.list_dlq(stream)
        assert parked.reason == DLQ_REASON_POISON
    finally:
        await client.delete(stream, f"{stream}.dlq")
        await client.aclose()
