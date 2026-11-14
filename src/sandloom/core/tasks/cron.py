"""Five-field cron expressions of ``@periodic`` (UTC), read without a scheduler.

``minute hour day-of-month month day-of-week``; every field is ``*``, a number,
a range ``a-b``, a list of them, each optionally with a step (``*/5``,
``10-50/10``). Numbers only (no month or weekday names), day-of-week
``0``-``6`` (0 is Sunday). A day-of-month and a day-of-week that are both
restricted are refused: schedulers disagree whether that means "and" or "or".

:func:`max_interval` is the longest gap between two consecutive slots, computed
when ``@periodic`` runs, so a health check can derive its tolerance from the
declaration instead of repeating the interval as a second constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

#: ``(name, lowest, highest)`` of each field, in order.
_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 6),
)

#: Days scanned for the gap across days: five years cover a leap day.
_SCAN_START = date(2024, 1, 1)
_SCAN_DAYS = 5 * 366


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A parsed expression: the values each field allows."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_any: bool
    day_of_week_any: bool

    def matches_day(self, day: date) -> bool:
        """Does a slot fall on ``day``?"""
        if day.month not in self.months:
            return False
        if not self.day_of_month_any:
            return day.day in self.days_of_month
        if not self.day_of_week_any:
            return (day.isoweekday() % 7) in self.days_of_week
        return True


def _parse_number(text: str, name: str, low: int, high: int, expression: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise ValueError(
            f"cron {expression!r}: {name} {text!r} is not a number "
            "(names such as 'mon' or 'jan' are not accepted)"
        ) from None
    if not low <= value <= high:
        raise ValueError(f"cron {expression!r}: {name} {value} is outside {low}-{high}")
    return value


def _parse_field(
    text: str, name: str, low: int, high: int, expression: str
) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            step = _parse_number(step_text, f"{name} step", 1, high, expression)
        if base == "*":
            start, stop = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _parse_number(first, name, low, high, expression)
            stop = _parse_number(last, name, low, high, expression)
            if start > stop:
                raise ValueError(
                    f"cron {expression!r}: {name} range {base!r} is reversed"
                )
        else:
            start = _parse_number(base, name, low, high, expression)
            stop = high if slash else start
        values.update(range(start, stop + 1, step))
    return values


def parse_cron(expression: str) -> CronSchedule:
    """Parse ``expression`` or raise ``ValueError`` saying what is wrong."""
    if not isinstance(expression, str):
        raise ValueError(f"cron must be a string, got {type(expression).__name__}")
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron {expression!r}: expected 5 fields "
            "'minute hour day-of-month month day-of-week'"
        )
    parsed = [
        _parse_field(text, name, low, high, expression)
        for text, (name, low, high) in zip(fields, _FIELDS, strict=True)
    ]
    day_of_month_any = fields[2] == "*"
    day_of_week_any = fields[4] == "*"
    if not day_of_month_any and not day_of_week_any:
        raise ValueError(
            f"cron {expression!r}: a day-of-month and a day-of-week are both "
            "set; use one of them (schedulers disagree whether that is 'and' or 'or')"
        )
    schedule = CronSchedule(
        expression=" ".join(fields),
        minutes=frozenset(parsed[0]),
        hours=frozenset(parsed[1]),
        days_of_month=frozenset(parsed[2]),
        months=frozenset(parsed[3]),
        days_of_week=frozenset(parsed[4]),
        day_of_month_any=day_of_month_any,
        day_of_week_any=day_of_week_any,
    )
    if not any(
        schedule.matches_day(_SCAN_START + timedelta(days=n)) for n in range(_SCAN_DAYS)
    ):
        raise ValueError(f"cron {expression!r}: no day ever matches it")
    return schedule


def max_interval(expression: str) -> timedelta:
    """The longest gap between two consecutive slots of ``expression``.

    A schedule with a single slot in the scanned years answers the scan
    length (the real gap is at least that long).
    """
    schedule = parse_cron(expression)
    slots = sorted(h * 60 + m for h in schedule.hours for m in schedule.minutes)
    longest = max((b - a for a, b in pairwise(slots)), default=0)
    previous: int | None = None  # minutes since the scan start
    for n in range(_SCAN_DAYS):
        if not schedule.matches_day(_SCAN_START + timedelta(days=n)):
            continue
        first, last = n * 1440 + slots[0], n * 1440 + slots[-1]
        if previous is not None:
            longest = max(longest, first - previous)
        previous = last
    if previous is None or longest == 0:
        return timedelta(days=_SCAN_DAYS)
    return timedelta(minutes=longest)


__all__ = ["CronSchedule", "max_interval", "parse_cron"]
