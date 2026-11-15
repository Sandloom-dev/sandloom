"""The slot helper of ``sandloom.driverkit.periodic`` (spec 8.2): the slot a send
is for, as an aware UTC instant (daylight saving time included), the one lease
name per periodic id and the claim of a slot on a lease store. No Celery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core.config.settings import settings
from sandloom.core.drivers.database_lease import DatabaseLeaseProvider
from sandloom.core.drivers.memory_lease import MemoryLeaseProvider
from sandloom.core.drivers.redis_lease import RedisLeaseProvider
from sandloom.core.infra.errors import (
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
)
from sandloom.core.infra.ports.lease import (
    MAX_LEASE_NAME_LENGTH,
    MIN_LEASE_TTL_SECONDS,
    Lease,
    LeaseStore,
)
from sandloom.core.sql_alchemy.models.lease import InfraLease
from sandloom.core.tasks.cron import parse_cron
from sandloom.driverkit.periodic import (
    SLOT_MARGIN_SECONDS,
    PeriodicSlot,
    check_slot_lease_name,
    claim_periodic_slot,
    periodic_slot,
    slot_lease_name,
)

_NEW_YORK = ZoneInfo("America/New_York")
_BASE = "sandloom:beat:lease:0123456789abcdef"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _slot(cron: str, now: str, zone: ZoneInfo | None = None) -> PeriodicSlot:
    slot = periodic_slot(parse_cron(cron), zone or ZoneInfo("UTC"), _utc(now))
    assert slot is not None
    return slot


# ------------------------------------------------------------------- slot math


def test_every_minute_the_slot_is_the_current_minute() -> None:
    slot = _slot("* * * * *", "2026-11-15T12:00:41")
    assert slot.key == "20261115T120000Z"
    # the claim ends a margin before the next slot, 19 s away
    assert slot.ttl_seconds == 19 - SLOT_MARGIN_SECONDS


def test_two_schedulers_name_a_late_send_by_the_slot_not_by_the_moment() -> None:
    early = _slot("0 3 * * *", "2026-11-15T03:00:01")
    late = _slot("0 3 * * *", "2026-11-15T09:30:00")
    assert early.key == late.key == "20261115T030000Z"
    # the claim of the late one still ends before the next slot
    assert late.ttl_seconds == 17 * 3600 + 30 * 60 - SLOT_MARGIN_SECONDS


def test_the_slot_before_the_first_of_the_day_is_yesterdays() -> None:
    assert _slot("30 23 * * *", "2026-11-15T00:10:00").key == "20261114T233000Z"


def test_a_schedule_with_weekday_and_hour_ranges() -> None:
    cron = "*/15 9-17 * * 1-5"
    # 2026-11-15 is a Sunday: the latest slot is Friday's last one
    assert _slot(cron, "2026-11-15T10:00:00").key == "20261113T174500Z"
    assert _slot(cron, "2026-11-16T09:20:00").key == "20261116T091500Z"


def test_a_date_that_recurs_every_four_years() -> None:
    slot = _slot("0 0 29 2 *", "2026-11-15T00:00:00")
    assert slot.key == "20240229T000000Z"
    assert slot.ttl_seconds is not None and slot.ttl_seconds > 365 * 24 * 3600


def test_the_slot_is_read_on_the_wall_clock_of_the_schedules_zone() -> None:
    moscow = ZoneInfo("Europe/Moscow")  # UTC+3, no daylight saving time
    slot = _slot("0 3 * * *", "2026-11-15T00:05:00", moscow)
    assert slot.key == "20261115T000000Z"


def test_a_slot_too_close_to_the_next_one_cannot_be_claimed() -> None:
    assert (
        _slot("* * * * *", "2026-11-15T12:00:55").ttl_seconds == MIN_LEASE_TTL_SECONDS
    )
    assert _slot("* * * * *", "2026-11-15T12:00:56").ttl_seconds is None


def test_a_claim_never_reaches_the_next_slot() -> None:
    # the claim ends a margin before the next slot, 30 s and 1 h 57 min 30 s away
    assert _slot("* * * * *", "2026-11-15T12:07:30").ttl_seconds == 30 - 2
    assert _slot("5 */2 * * *", "2026-11-15T12:07:30").ttl_seconds == 7050 - 2


def test_the_repeated_hour_of_a_fall_back_has_two_slots() -> None:
    # 2026-11-01: 01:00 EDT is 05:00Z and 01:00 EST is 06:00Z
    first = _slot("0 * * * *", "2026-11-01T05:30:00", _NEW_YORK)
    second = _slot("0 * * * *", "2026-11-01T06:30:00", _NEW_YORK)
    assert first.key == "20261101T050000Z"
    assert second.key == "20261101T060000Z"
    assert _slot("0 * * * *", "2026-11-01T05:59:59", _NEW_YORK).key == first.key
    assert _slot("0 * * * *", "2026-11-01T06:00:00", _NEW_YORK).key == second.key
    # the claim of the first ends before the second (not at the next wall hour)
    assert first.ttl_seconds == 30 * 60 - SLOT_MARGIN_SECONDS


def test_a_daily_slot_inside_the_repeated_hour_runs_on_both_readings() -> None:
    cron = "30 1 * * *"
    assert _slot(cron, "2026-11-01T05:40:00", _NEW_YORK).key == "20261101T053000Z"
    # 01:10 EST (second pass): the 01:30 EDT run of an hour ago is the latest
    assert _slot(cron, "2026-11-01T06:10:00", _NEW_YORK).key == "20261101T053000Z"
    assert _slot(cron, "2026-11-01T06:40:00", _NEW_YORK).key == "20261101T063000Z"


def test_the_gap_of_a_spring_forward_is_one_slot() -> None:
    # 2026-03-08: 02:00-03:00 does not exist in New York
    cron = "30 2 * * *"
    keys = {
        _slot(cron, now, _NEW_YORK).key
        for now in (
            "2026-03-08T07:30:00",
            "2026-03-08T08:15:00",
            "2026-03-09T05:00:00",
        )
    }
    assert len(keys) == 1
    hourly = {
        _slot("0 * * * *", now, _NEW_YORK).key
        for now in ("2026-03-08T07:00:00", "2026-03-08T07:59:00")
    }
    assert len(hourly) == 1  # 02:00 (gap) and 03:00 EDT are one instant


# ------------------------------------------------------------------ lease names


def test_the_slot_lease_name_is_one_per_id_and_bounded() -> None:
    cron = parse_cron("* * * * *")
    utc = ZoneInfo("UTC")
    one = slot_lease_name(_BASE, "billing.sweep", cron, utc)
    assert one == slot_lease_name(_BASE, "billing.sweep", cron, utc)
    assert one != slot_lease_name(_BASE, "identity.purge", cron, utc)
    # a changed schedule starts a new name: an old claim cannot hold it back
    assert one != slot_lease_name(_BASE, "billing.sweep", parse_cron("0 * * * *"), utc)
    assert one != slot_lease_name(_BASE, "billing.sweep", cron, _NEW_YORK)
    assert len(slot_lease_name(_BASE, "x" * 500, cron, utc)) == len(_BASE) + 22


def test_a_scheduler_lease_name_must_leave_room_for_the_slot_suffix() -> None:
    check_slot_lease_name("x" * (MAX_LEASE_NAME_LENGTH - 22))
    with pytest.raises(ValueError, match="at most 233"):
        check_slot_lease_name("x" * (MAX_LEASE_NAME_LENGTH - 21))


# ----------------------------------------------------------------------- claims


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _memory() -> tuple[_Clock, MemoryLeaseProvider]:
    clock = _Clock()
    return clock, MemoryLeaseProvider(
        name="beat", driver="memory", options={}, clock=clock
    )


async def _claim(store: LeaseStore, slot: PeriodicSlot, holder: str = "a:1") -> str:
    claim = await claim_periodic_slot(
        store, "name", slot, holder=holder, fence=7, limit_seconds=1.0
    )
    return claim.state


async def test_a_slot_is_claimed_once_and_the_next_one_finds_the_name_free() -> None:
    clock, store = _memory()
    first = _slot("* * * * *", "2026-11-15T12:00:10")
    assert await _claim(store, first) == "claimed"
    assert await _claim(store, first, holder="b:2") == "taken"
    held = await store.holder("name")
    assert held is not None
    assert held.owner == "slot:20261115T120000Z:fence:7:a:1"
    clock.now += 60  # the next slot: the claim of the first has ended
    assert await _claim(store, _slot("* * * * *", "2026-11-15T12:01:10")) == "claimed"


async def test_ten_minutely_slots_create_one_lease_name_in_memory() -> None:
    clock, store = _memory()
    for minute in range(10):
        slot = _slot("* * * * *", f"2026-11-15T12:{minute:02d}:10")
        assert await _claim(store, slot) == "claimed"
        clock.now += 60
    assert len(store._held) == 1


class _Faulty(MemoryLeaseProvider):
    def __init__(self, error: BaseException | None, delay: float = 0.0) -> None:
        super().__init__(name="beat", driver="memory", options={})
        self.error = error
        self.delay = delay

    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)


@pytest.mark.parametrize(
    "error",
    [
        Unavailable("down", capability="lease"),
        Timeout("slow", capability="lease"),
        TimeoutError("slow"),
    ],
)
async def test_a_store_that_does_not_answer_lets_the_slot_through(
    error: BaseException,
) -> None:
    slot = _slot("* * * * *", "2026-11-15T12:00:10")
    claim = await claim_periodic_slot(
        _Faulty(error), "name", slot, holder="a:1", fence=1, limit_seconds=1.0
    )
    assert claim.state == "unknown" and claim.may_send


async def test_a_store_that_takes_too_long_lets_the_slot_through() -> None:
    slot = _slot("* * * * *", "2026-11-15T12:00:10")
    claim = await claim_periodic_slot(
        _Faulty(None, delay=1.0),
        "name",
        slot,
        holder="a:1",
        fence=1,
        limit_seconds=0.01,
    )
    assert claim.state == "unknown"


@pytest.mark.parametrize(
    "error",
    [
        InvalidArgument("bad name", capability="lease"),
        PermissionDenied("no", capability="lease"),
    ],
)
async def test_a_store_that_refuses_the_claim_sends_nothing(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    slot = _slot("* * * * *", "2026-11-15T12:00:10")
    claim = await claim_periodic_slot(
        _Faulty(error), "name", slot, holder="a:1", fence=1, limit_seconds=1.0
    )
    assert claim.state == "refused" and not claim.may_send
    assert any(record.levelname == "ERROR" for record in caplog.records)


async def test_a_slot_too_close_to_the_next_is_sent_without_a_claim() -> None:
    _clock, store = _memory()
    slot = _slot("* * * * *", "2026-11-15T12:00:58")
    claim = await claim_periodic_slot(
        store, "name", slot, holder="a:1", fence=1, limit_seconds=1.0
    )
    assert claim.state == "late" and claim.may_send
    assert await store.holder("name") is None


# ---------------------------------- the same on the three lease stores (names)


async def _ten_slots(store: LeaseStore, name: str) -> None:
    """Ten minutely slots, each claimed, refused a second time and given back
    (the TTL of the claim has run out when the next slot comes)."""
    for minute in range(10):
        slot = _slot("* * * * *", f"2026-11-15T12:{minute:02d}:10")
        claim = await claim_periodic_slot(
            store, name, slot, holder="a:1", fence=1, limit_seconds=5.0
        )
        assert claim.state == "claimed" and claim.lease is not None
        again = await claim_periodic_slot(
            store, name, slot, holder="b:2", fence=1, limit_seconds=5.0
        )
        assert again.state == "taken"
        assert await store.release(claim.lease) is True


async def test_ten_minutely_slots_create_one_lease_name_on_redis() -> None:
    store = RedisLeaseProvider(
        name="beat", driver="redis", options={"url": settings.cache_url}
    )
    base = f"slot-names-{id(store)}"
    name = slot_lease_name(
        base, "billing.sweep", parse_cron("* * * * *"), ZoneInfo("UTC")
    )
    await store.aopen()
    client = await store._redis()
    pattern = f"{store._opts.key_prefix}{{{base}:slot:*}}:fence"
    try:
        await _ten_slots(store, name)
        fences = [key async for key in client.scan_iter(match=pattern)]
        assert len(fences) == 1  # the no-TTL key of the name: one, not ten
    finally:
        for key in store._keys(name):
            await client.delete(key)
        await store.aclose()


@pytest_asyncio.fixture
async def sqlite_url(tmp_path) -> str:  # type: ignore[no-untyped-def]
    url = f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(InfraLease.__table__.create, checkfirst=True)  # type: ignore[attr-defined]
    finally:
        await engine.dispose()
    return url


async def test_ten_minutely_slots_create_one_lease_row_in_a_database(
    sqlite_url: str,
) -> None:
    store = DatabaseLeaseProvider(
        name="beat", driver="database", options={"url": sqlite_url}
    )
    name = slot_lease_name(
        _BASE, "billing.sweep", parse_cron("* * * * *"), ZoneInfo("UTC")
    )
    await store.aopen()
    try:
        await _ten_slots(store, name)
    finally:
        await store.aclose()
    engine = create_async_engine(sqlite_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.scalar(select(func.count()).select_from(InfraLease))
    finally:
        await engine.dispose()
    assert rows == 1  # the rows are never deleted: one per id, not one per slot
