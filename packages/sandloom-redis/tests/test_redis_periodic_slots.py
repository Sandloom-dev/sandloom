"""The slot lease names of ``sandloom.driverkit.periodic`` on the ``redis`` lease
driver: ten minutely slots leave one lease name behind, not ten (the memory and
database stores run the same claim in the kernel's ``test_driverkit_periodic.py``)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.config.settings import settings
from sandloom.core.tasks.cron import parse_cron
from sandloom.driverkit.periodic import (
    claim_periodic_slot,
    periodic_slot,
    slot_lease_name,
)


async def test_ten_minutely_slots_create_one_lease_name_on_redis() -> None:
    store = RedisLeaseProvider(
        name="beat", driver="redis", options={"url": settings.cache_url}
    )
    cron = parse_cron("* * * * *")
    base = f"slot-names-{id(store)}"
    name = slot_lease_name(base, "billing.sweep", cron, ZoneInfo("UTC"))
    await store.aopen()
    client = await store._redis()
    pattern = f"{store._opts.key_prefix}{{{base}:slot:*}}:fence"
    try:
        for minute in range(10):
            now = datetime(2026, 11, 15, 12, minute, 10, tzinfo=UTC)
            slot = periodic_slot(cron, ZoneInfo("UTC"), now)
            assert slot is not None
            claim = await claim_periodic_slot(
                store, name, slot, holder="a:1", fence=1, limit_seconds=5.0
            )
            assert claim.state == "claimed" and claim.lease is not None
            again = await claim_periodic_slot(
                store, name, slot, holder="b:2", fence=1, limit_seconds=5.0
            )
            assert again.state == "taken"
            assert await store.release(claim.lease) is True
        fences = [key async for key in client.scan_iter(match=pattern)]
        assert len(fences) == 1  # the no-TTL key of the name: one, not ten
    finally:
        for key in store._keys(name):
            await client.delete(key)
        await store.aclose()
