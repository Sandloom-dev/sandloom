"""The cron slots the Celery scheduler deduplicates a run by (spec 8.2): the
latest slot at or before the moment of the send, named the same by every
scheduler, and how long its claim lives."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from celery import Celery
from celery.schedules import crontab

from sandloom.core.drivers.celery_worker import (
    SLOT_MARGIN_SECONDS,
    CronFields,
    schedule_wall_clock,
    slot_lease_name,
    slot_of,
)

_APP = Celery("slots", broker="memory://", set_as_current=False)


def _fields(**cron: str) -> CronFields:
    return CronFields.of(crontab(app=_APP, **cron))


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_every_minute_the_slot_is_the_current_minute() -> None:
    slot = slot_of(_fields(), _at("2026-11-15T12:00:41"))
    assert slot is not None
    assert slot.key == "202611151200"
    # the claim lives to the next slot (19 s later, rounded up) and the margin
    assert slot.ttl_seconds == 20 + SLOT_MARGIN_SECONDS


def test_two_schedulers_name_a_late_send_by_the_slot_not_by_the_moment() -> None:
    fields = _fields(minute="0", hour="3")
    early = slot_of(fields, _at("2026-11-15T03:00:01"))
    late = slot_of(fields, _at("2026-11-15T09:30:00"))
    assert early is not None and late is not None
    assert early.key == late.key == "202611150300"
    # the claim outlives every send for that slot: it ends after the next one
    assert late.ttl_seconds > (17 * 3600 + 30 * 60)


def test_the_slot_before_the_first_of_the_day_is_yesterdays() -> None:
    slot = slot_of(_fields(minute="30", hour="23"), _at("2026-11-15T00:10:00"))
    assert slot is not None and slot.key == "202611142330"


def test_a_schedule_with_weekday_and_hour_ranges() -> None:
    fields = _fields(minute="*/15", hour="9-17", day_of_week="1-5")
    # 2026-11-15 is a Sunday: the latest slot is Friday's last one
    friday = slot_of(fields, _at("2026-11-15T10:00:00"))
    assert friday is not None and friday.key == "202611131745"
    monday = slot_of(fields, _at("2026-11-16T09:20:00"))
    assert monday is not None and monday.key == "202611160915"


def test_a_date_that_recurs_every_four_years() -> None:
    fields = _fields(minute="0", hour="0", day_of_month="29", month_of_year="2")
    slot = slot_of(fields, _at("2026-11-15T00:00:00"))
    assert slot is not None and slot.key == "202402290000"


def test_the_wall_clock_is_the_schedules_timezone() -> None:
    app = Celery("tz", broker="memory://", set_as_current=False)
    app.conf.timezone = "Europe/Moscow"
    schedule = crontab(app=app, minute="0", hour="3")
    wall = schedule_wall_clock(schedule, datetime(2026, 11, 15, 0, 5, tzinfo=UTC))
    assert wall == _at("2026-11-15T03:05:00")


def test_the_slot_lease_name_is_bounded_and_per_id_and_slot() -> None:
    base = "sandloom:beat:lease:0123456789abcdef"
    one = slot_lease_name(base, "billing.sweep", "202611151200")
    assert one == slot_lease_name(base, "billing.sweep", "202611151200")
    assert one != slot_lease_name(base, "billing.sweep", "202611151201")
    assert one != slot_lease_name(base, "identity.purge", "202611151200")
    assert len(slot_lease_name(base, "x" * 500, "202611151200")) < 100


@pytest.mark.parametrize("cron", [{"minute": "*"}, {"minute": "5", "hour": "*/2"}])
def test_the_claim_never_outlives_its_slot_by_more_than_the_margin(
    cron: dict[str, str],
) -> None:
    fields = _fields(**cron)
    now = _at("2026-11-15T12:07:30")
    slot = slot_of(fields, now)
    assert slot is not None
    nxt = fields.following(fields.latest(now) or now)
    assert nxt is not None
    assert (
        slot.ttl_seconds - SLOT_MARGIN_SECONDS == int((nxt - now).total_seconds()) + 1
    )
