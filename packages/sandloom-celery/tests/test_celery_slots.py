"""The Celery adapter of the cron slots (spec 8.2): a ``crontab`` read as a
``CronSchedule``, and the scheduler that sends each slot once through the
driverkit's slot helper (``sandloom.driverkit.periodic``, tested in
``test_driverkit_periodic``)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery
from celery.schedules import crontab
from sandloom_celery.worker import (
    BeatLease,
    CeleryWorkerHost,
    LeasedScheduler,
    _cron_fields,
    cron_schedule_of,
)

from sandloom.core.drivers.memory_lease import MemoryLeaseProvider
from sandloom.core.tasks.cron import parse_cron
from sandloom.driverkit import InvalidArgument, Lease, PermissionDenied, Unavailable

_APP = Celery("slots", broker="memory://", set_as_current=False)
_NY_APP = Celery("slots-ny", broker="memory://", set_as_current=False)
_NY_APP.conf.timezone = "America/New_York"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ---------------------------------------------------------------- the adapter


@pytest.mark.parametrize(
    "cron",
    [
        "* * * * *",
        "*/15 9-17 * * 1-5",
        "5 */2 * * *",
        "0 0 29 2 *",
        "30 23 1,15 * *",
        "0 4 * 1-3 0",
    ],
)
def test_a_crontab_of_a_declared_cron_reads_back_as_the_same_schedule(
    cron: str,
) -> None:
    declared = parse_cron(cron)
    read = cron_schedule_of(crontab(app=_APP, **_cron_fields(cron)))
    assert read is not None
    assert read.minutes == declared.minutes
    assert read.hours == declared.hours
    assert read.days_of_month == declared.days_of_month
    assert read.months == declared.months
    assert read.days_of_week == declared.days_of_week
    for day_of in (1, 8, 15, 22, 29):  # the day matching agrees
        day = datetime(2026, 11, day_of).date()
        assert read.matches_day(day) == declared.matches_day(day)


def test_a_crontab_with_both_day_fields_restricted_has_no_slot() -> None:
    both = crontab(app=_APP, minute="0", day_of_month="1", day_of_week="1")
    assert cron_schedule_of(both) is None


# ----------------------------------------------------- the scheduler, two of it


class _Time:
    """One moment for the schedulers' wall clock and the lease store's clock."""

    def __init__(self, text: str) -> None:
        self.at = _utc(text)
        self._origin = self.at

    def wall(self) -> datetime:
        return self.at

    def mono(self) -> float:
        return 1000.0 + (self.at - self._origin).total_seconds()

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class _Store(MemoryLeaseProvider):
    """The memory lease on the shared clock, which can answer with an error."""

    def __init__(self, time: _Time) -> None:
        super().__init__(name="beat", driver="memory", options={}, clock=time.mono)
        self.error: Exception | None = None

    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        if self.error is not None and ":slot:" in name:
            raise self.error
        return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)

    @property
    def slot_names(self) -> list[str]:
        return [name for name in self._held if ":slot:" in name]


class _Beat:
    """A Celery scheduler's ``apply_async`` / ``reserve`` as the beat calls them,
    recording the sends."""

    def __init__(self, sent: list[tuple[str, str, str]], who: str, time: _Time) -> None:
        self.sent = sent
        self.who = who
        self.time = time
        self.reserved: list[str] = []
        self.fails: Exception | None = None

    def reserve(self, entry: Any) -> Any:
        self.reserved.append(entry.name)
        return entry

    def apply_async(
        self, entry: Any, producer: Any = None, advance: bool = True, **_: Any
    ) -> str:
        if self.fails is not None:
            raise self.fails
        self.sent.append((self.who, entry.name, self.time.at.isoformat()))
        return "id"

    def tick(self) -> float:
        return 1.0

    def close(self) -> None:
        pass


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


class _Pair:
    def __init__(self, start: str = "2026-11-15T12:00:10", ttl: int = 1000) -> None:
        self.time = _Time(start)
        self.store = _Store(self.time)
        self.sent: list[tuple[str, str, str]] = []
        self.inner: dict[str, _Beat] = {}
        self.sched: dict[str, LeasedScheduler] = {}
        for who, identity in (("a", "a:1"), ("b", "b:2")):
            lease = BeatLease(
                self.store,
                "sandloom:beat:lease:test",
                ttl_seconds=ttl,
                runner=_run,
                clock=self.time.mono,
                identity=identity,
            )
            inner = _Beat(self.sent, who, self.time)
            self.inner[who] = inner
            self.sched[who] = LeasedScheduler(inner, lease, wall_clock=self.time.wall)

    def send(self, who: str, entry: Any) -> None:
        self.inner[who].apply_async(entry)

    def who_sent(self) -> list[str]:
        return [who for who, _name, _at in self.sent]


def _entry(
    cron: str = "*", name: str = "billing.sweep", app: Celery = _APP, **fields: str
) -> Any:
    return SimpleNamespace(name=name, schedule=crontab(minute=cron, app=app, **fields))


def test_ten_minutely_slots_are_sent_once_each_under_one_lease_name() -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    for _ in range(10):
        pair.send("a", _entry())
        pair.send("a", _entry())  # the same slot again (a restart, a re-tick)
        pair.send("b", _entry())  # a standby sends nothing
        pair.time.advance(60)
    assert pair.who_sent() == ["a"] * 10
    assert len(pair.store.slot_names) == 1  # before: one name per slot (10)
    assert len(pair.inner["a"].reserved) == 10  # the refused re-tick advanced


def test_a_takeover_inside_a_slot_does_not_resend_it() -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    pair.send("a", _entry())
    pair.sched["a"].close()  # a standby takes over at once
    pair.time.advance(5)
    assert pair.sched["b"].lease.ensure() is True
    pair.send("b", _entry())
    assert pair.who_sent() == ["a"]
    pair.time.advance(60)  # the next slot is b's
    pair.send("b", _entry())
    assert pair.who_sent() == ["a", "b"]


def test_a_takeover_after_the_holder_died_does_not_resend_the_slot() -> None:
    pair = _Pair(ttl=30)
    assert pair.sched["a"].lease.ensure() is True
    pair.send("a", _entry("0", hour="3"))  # sent late, for the 03:00 slot
    pair.time.advance(31)  # a dies without releasing; its lease runs out
    assert pair.sched["b"].lease.ensure() is True
    assert pair.sched["b"].lease.fence == 2
    pair.send("b", _entry("0", hour="3"))
    assert pair.who_sent() == ["a"]


def test_a_scheduler_killed_between_claim_and_send_loses_that_slot() -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    claim = pair.sched["a"].lease.claim_slot(
        "billing.sweep",
        cron_schedule_of(_entry().schedule),  # type: ignore[arg-type]
        _entry().schedule.tz,
        _slot_now(pair),
    )
    assert claim.state == "claimed"  # ... and the process dies before the send
    pair.time.advance(5)
    pair.sched["a"].close()
    assert pair.sched["b"].lease.ensure() is True
    pair.send("b", _entry())
    assert pair.sent == []  # documented: that slot is not sent by anyone


def _slot_now(pair: _Pair) -> Any:
    from sandloom.driverkit.periodic import periodic_slot

    entry = _entry()
    cron = cron_schedule_of(entry.schedule)
    assert cron is not None
    slot = periodic_slot(cron, entry.schedule.tz, pair.time.at)
    assert slot is not None
    return slot


def test_the_repeated_hour_of_a_fall_back_runs_once_on_each_reading() -> None:
    # America/New_York, 2026-11-01: 01:00 EDT is 05:00Z, 01:00 EST is 06:00Z
    pair = _Pair("2026-11-01T04:30:00", ttl=100000)
    assert pair.sched["a"].lease.ensure() is True
    hourly = _entry("0", app=_NY_APP)
    for text in ("2026-11-01T05:00:03", "2026-11-01T06:00:03", "2026-11-01T07:00:03"):
        pair.time.at = _utc(text)
        pair.send("a", hourly)
        pair.send("a", hourly)  # a second send of the same instant: refused
    assert [at for _w, _n, at in pair.sent] == [
        "2026-11-01T05:00:03+00:00",
        "2026-11-01T06:00:03+00:00",
        "2026-11-01T07:00:03+00:00",
    ]


def test_the_gap_of_a_spring_forward_does_not_double_send() -> None:
    pair = _Pair("2026-03-08T06:30:00", ttl=100000)
    assert pair.sched["a"].lease.ensure() is True
    daily = _entry("30", app=_NY_APP, hour="2")  # 02:30 does not exist that day
    for text in ("2026-03-08T07:30:05", "2026-03-08T07:30:09", "2026-03-08T07:45:00"):
        pair.time.at = _utc(text)
        pair.send("a", daily)
    assert len(pair.sent) == 1


# ------------------------------------------------- failures of the lease store


def test_a_store_that_does_not_answer_lets_the_slot_through() -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    pair.store.error = Unavailable("down", capability="lease")
    pair.send("a", _entry())
    assert pair.who_sent() == ["a"]


@pytest.mark.parametrize(
    "error",
    [
        InvalidArgument("name too long", capability="lease"),
        PermissionDenied("no access", capability="lease"),
    ],
)
def test_a_store_that_refuses_the_claim_sends_nothing(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    pair.store.error = error
    pair.send("a", _entry())
    assert pair.sent == []
    assert pair.inner["a"].reserved == ["billing.sweep"]
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_a_scheduler_lease_name_too_long_for_a_slot_refuses_to_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pair = _Pair()
    host = CeleryWorkerHost(lambda: _APP)
    boot: Any = SimpleNamespace()  # never reached: the check comes first
    assert host.run_scheduler(boot, lease=pair.store, lease_name="x" * 240) == 1
    assert "refusing to schedule" in caplog.text
    assert "at most 233" in caplog.text


# ------------------------------------------------------ a send that fails


def test_a_send_that_may_have_been_published_keeps_the_claim() -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    pair.inner["a"].fails = ConnectionError("broker dropped the connection")
    with pytest.raises(ConnectionError):
        pair.send("a", _entry())
    pair.sched["a"].close()
    pair.time.advance(5)
    assert pair.sched["b"].lease.ensure() is True
    pair.send("b", _entry())
    assert pair.sent == []  # at-most-once for the slot: a run may be lost, not doubled


@pytest.mark.parametrize("error", [InvalidArgument("not encodable", capability="x")])
def test_a_send_refused_before_the_broker_gives_the_slot_back(
    error: Exception,
) -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    pair.inner["a"].fails = error
    with pytest.raises(InvalidArgument):
        pair.send("a", _entry())
    pair.sched["a"].close()
    pair.time.advance(5)
    assert pair.sched["b"].lease.ensure() is True
    pair.send("b", _entry())
    assert pair.who_sent() == ["b"]


def test_an_entry_that_is_not_a_cron_one_is_sent_without_a_claim() -> None:
    pair = _Pair()
    entry = SimpleNamespace(name="interval", schedule=object())
    pair.send("a", entry)  # not even holding the lease: not ours to judge
    assert pair.who_sent() == ["a"]
    assert pair.store.slot_names == []


def test_a_slot_claim_names_the_slot_and_the_fence_of_its_holder() -> None:
    pair = _Pair()
    assert pair.sched["a"].lease.ensure() is True
    pair.send("a", _entry())
    owners = [pair.store._held[name].owner for name in pair.store.slot_names]
    assert owners == ["slot:20261115T120000Z:fence:1:a:1"]
