"""Two Celery schedulers on one real Redis lease send a cron slot once (spec 8.2).

The scheduler logic runs on a fake lease store in ``test_celery_slots``; the
helper's claims on the three stores in ``test_driverkit_periodic``. This one
puts the two together on Redis, where the claim's TTL is the store's own."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from celery import Celery
from celery.schedules import crontab
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.config.settings import settings
from sandloom.core.drivers.celery_worker import BeatLease, LeasedScheduler

_APP = Celery("dedupe", broker="memory://", set_as_current=False)


class _Beat:
    def __init__(self, sent: list[str], who: str) -> None:
        self.sent = sent
        self.who = who

    def reserve(self, entry: Any) -> Any:
        return entry

    def apply_async(
        self, entry: Any, producer: Any = None, advance: bool = True, **_: Any
    ) -> str:
        self.sent.append(self.who)
        return "id"

    def tick(self) -> float:
        return 1.0

    def close(self) -> None:
        pass


async def test_two_schedulers_share_one_slot_on_real_redis() -> None:
    store = RedisLeaseProvider(
        name="beat", driver="redis", options={"url": settings.cache_url}
    )
    await store.aopen()
    loop = asyncio.get_running_loop()

    def runner(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)

    name = f"sandloom:beat:lease:slot-test-{os.getpid()}-{id(store)}"
    wall = datetime(2026, 11, 15, 12, 0, 10, tzinfo=UTC)
    sent: list[str] = []
    first = BeatLease(store, name, ttl_seconds=3, runner=runner, identity="a:1")
    second = BeatLease(store, name, ttl_seconds=3, runner=runner, identity="b:2")
    a_inner, b_inner = _Beat(sent, "a"), _Beat(sent, "b")
    LeasedScheduler(a_inner, first, wall_clock=lambda: wall)
    LeasedScheduler(b_inner, second, wall_clock=lambda: wall)
    entry = SimpleNamespace(
        name="billing.sweep", schedule=crontab(minute="*", app=_APP)
    )
    client = await store._redis()
    try:
        assert await asyncio.to_thread(first.ensure) is True
        await asyncio.to_thread(a_inner.apply_async, entry)
        await asyncio.to_thread(first.release)
        second._holder._retry_at = 0.0
        assert await asyncio.to_thread(second.ensure) is True
        await asyncio.to_thread(b_inner.apply_async, entry)  # same slot: taken
        assert sent == ["a"]
        held = [
            key
            async for key in client.scan_iter(
                match=f"{store._opts.key_prefix}{{{name}:slot:*}}:lease"
            )
        ]
        assert len(held) == 1  # one name for the id
    finally:
        await asyncio.to_thread(second.release)
        async for key in client.scan_iter(
            match=f"{store._opts.key_prefix}{{{name}*}}:*"
        ):
            await client.delete(key)
        await store.aclose()
