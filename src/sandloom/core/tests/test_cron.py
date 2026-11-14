"""The five-field cron expressions of ``@periodic`` and their longest gap."""

from __future__ import annotations

from datetime import timedelta

import pytest

from sandloom.core.tasks.cron import max_interval, parse_cron


@pytest.mark.parametrize(
    ("expression", "interval"),
    [
        ("*/5 * * * *", timedelta(minutes=5)),
        ("* * * * *", timedelta(minutes=1)),
        ("0 * * * *", timedelta(hours=1)),
        ("0 3 * * *", timedelta(days=1)),
        ("0,30 8-17 * * *", timedelta(hours=14, minutes=30)),
        ("15 4 * * 1", timedelta(days=7)),
        ("0 0 1 * *", timedelta(days=31)),
        ("0 0 1 1 *", timedelta(days=366)),
        ("0 0 29 2 *", timedelta(days=1461)),
    ],
)
def test_the_longest_gap_between_two_slots(
    expression: str, interval: timedelta
) -> None:
    assert max_interval(expression) == interval


def test_the_declared_intervals_of_the_bundled_modules() -> None:
    assert max_interval("*/5 * * * *").total_seconds() == 300
    assert max_interval("0 * * * *").total_seconds() == 3600
    assert max_interval("0 3 * * *").total_seconds() == 86400


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("* * * *", "expected 5 fields"),
        ("* * * * * *", "expected 5 fields"),
        ("60 * * * *", "outside 0-59"),
        ("* 24 * * *", "outside 0-23"),
        ("* * 0 * *", "outside 1-31"),
        ("* * * 13 *", "outside 1-12"),
        ("* * * * 7", "outside 0-6"),
        ("* * * * mon", "not a number"),
        ("*/0 * * * *", "outside 1-59"),
        ("5-1 * * * *", "reversed"),
        ("0 0 1 * 1", "both set"),
        ("0 0 31 2 *", "no day ever matches"),
    ],
)
def test_a_bad_expression_is_refused(expression: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_cron(expression)


def test_a_cron_must_be_a_string() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        parse_cron(300)  # type: ignore[arg-type]


def test_fields_expand_to_their_values() -> None:
    schedule = parse_cron("10-50/20,59 */12 * 1,6 0")
    assert schedule.minutes == {10, 30, 50, 59}
    assert schedule.hours == {0, 12}
    assert schedule.months == {1, 6}
    assert schedule.days_of_week == {0}
