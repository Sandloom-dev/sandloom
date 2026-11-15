"""Cron slots and their claims, for a scheduler a driver package hosts (spec 8.2).

A scheduler that sends a periodic task deduplicates a run by ``(periodic id,
slot)``: the slot is the cron slot a run is for, the latest one at or before the
moment of the send, so two schedulers that send the same slot (a takeover, a
send that blocked past the scheduler lease) name it identically whatever their
own clocks of the last run say. This module holds what is not specific to one
scheduler:

* :func:`periodic_slot` names the slot of a send on aware UTC instants (a slot of
  a zone with daylight saving time is an instant, so the repeated hour of a
  fall-back runs twice and the gap of a spring-forward once);
* :func:`slot_lease_name` is the ONE lease name of a periodic id (the number of
  names is the number of periodic ids, never a name per slot);
* :func:`claim_periodic_slot` claims a slot on a lease store: an ``acquire`` of
  that name whose TTL ends a margin before the next slot, so the next slot
  finds the name free again.

The claim is granted once per slot: a store that answers "held" (by anyone) means
another scheduler sent the slot. A store that does not answer (``Unavailable``,
``Timeout``) lets the slot through: a run may repeat, never get lost. A refusal of
any other kind (a name or an owner the store does not accept) sends nothing and
is logged as an error. A slot whose next one is closer than the margin cannot be
claimed without overlapping it: it is sent without a claim (``late``). A
scheduler killed between the claim and the send loses that slot.

The cron fields are a :class:`~sandloom.core.tasks.cron.CronSchedule`; the zone
is the scheduler's own.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as clock_time
from typing import Literal

from sandloom.core.infra.errors import InfraError, Timeout, Unavailable
from sandloom.core.infra.ports.lease import Lease, LeaseStore
from sandloom.core.tasks.cron import CronSchedule
from sandloom.driverkit.lease_limits import (
    MAX_LEASE_NAME_LENGTH,
    MIN_LEASE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

#: A claim ends this long before the next slot: the lease backend and the
#: scheduler differ by the latency of the claim and by clock drift.
SLOT_MARGIN_SECONDS = 2
#: A schedule with no later slot (a date that does not recur) keeps its claim
#: this long.
_NO_NEXT_SLOT_SECONDS = 366 * 24 * 3600
#: Feb 29 on a given weekday recurs within 28 years.
_SEARCH_DAYS = 29 * 366
#: What follows the scheduler's lease name in a slot lease name (``:slot:`` and
#: the 16 hex characters of the id's digest).
_SLOT_NAME_SUFFIX = len(":slot:") + 16

SlotState = Literal["claimed", "taken", "not_held", "unknown", "late", "refused"]


@dataclass(frozen=True, slots=True)
class PeriodicSlot:
    """The cron slot a run is for and how long its claim may live."""

    #: The slot, an aware UTC instant.
    at: datetime
    #: ``at`` as text (``20261101T050000Z``).
    key: str
    #: Seconds from now to a margin before the next slot; ``None`` when the next
    #: slot is too close for a claim of at least ``MIN_LEASE_TTL_SECONDS``.
    ttl_seconds: int | None


@dataclass(frozen=True, slots=True)
class SlotClaim:
    """The answer of :func:`claim_periodic_slot` (or of a scheduler that does not
    hold its lease: ``not_held``)."""

    #: ``claimed`` (send), ``taken`` (another scheduler sent this slot),
    #: ``not_held`` (the scheduler's lease is not ours: send nothing),
    #: ``unknown`` (the store did not answer: send), ``late`` (the next slot is
    #: too close to claim: send) or ``refused`` (the store refused the claim:
    #: send nothing).
    state: SlotState
    lease: Lease | None = None

    @property
    def may_send(self) -> bool:
        return self.state in ("claimed", "unknown", "late")


# ------------------------------------------------------------------ slot math


def _offset_spread(zone: tzinfo, wall: datetime) -> timedelta:
    """How far the UTC offset of ``zone`` moves within a day of ``wall``."""
    offsets = [
        (wall + timedelta(days=days)).replace(tzinfo=zone).utcoffset() or timedelta(0)
        for days in (-1, 0, 1)
    ]
    return max(offsets) - min(offsets)


def _instants(wall: datetime, zone: tzinfo) -> list[datetime]:
    """The UTC instants of a naive ``wall`` clock reading: one, two for a reading
    that happens twice (the hour a fall-back repeats), and one for a reading that
    never happens (the gap of a spring-forward)."""
    found: list[datetime] = []
    for fold in (0, 1):
        at = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if at not in found:
            found.append(at)
    if len(found) == 1:
        return found
    real = [at for at in found if at.astimezone(zone).replace(tzinfo=None) == wall]
    return real or found[:1]


def _walls(
    schedule: CronSchedule, start: datetime, *, back: bool
) -> Iterator[datetime]:
    """The naive wall-clock slots of ``schedule`` from ``start`` (inclusive)
    backwards or forwards, in order."""
    hours = sorted(schedule.hours, reverse=back)
    minutes = sorted(schedule.minutes, reverse=back)
    sign = -1 if back else 1
    for days in range(_SEARCH_DAYS):
        try:
            day = start.date() + timedelta(days=sign * days)
        except OverflowError:
            return
        if not schedule.matches_day(day):
            continue
        for hour in hours:
            for minute in minutes:
                wall = datetime.combine(day, clock_time(hour, minute))
                if (wall <= start) if back else (wall >= start):
                    yield wall


def _latest(schedule: CronSchedule, zone: tzinfo, now: datetime) -> datetime | None:
    """The latest slot at or before ``now`` (aware), as a UTC instant."""
    local = now.astimezone(zone).replace(tzinfo=None)
    best: datetime | None = None
    best_wall: datetime | None = None
    bound = _offset_spread(zone, local)
    try:
        for wall in _walls(schedule, local + bound, back=True):
            if best_wall is not None and wall < best_wall - bound:
                break  # no older wall can be a later instant
            for at in _instants(wall, zone):
                if at <= now and (best is None or at > best):
                    best, best_wall = at, wall
                    bound = _offset_spread(zone, wall)
    except OverflowError:
        pass
    return best


def _following(
    schedule: CronSchedule, zone: tzinfo, after: datetime
) -> datetime | None:
    """The first slot strictly after ``after`` (aware), as a UTC instant."""
    local = after.astimezone(zone).replace(tzinfo=None)
    best: datetime | None = None
    best_wall: datetime | None = None
    bound = _offset_spread(zone, local)
    try:
        for wall in _walls(schedule, local - bound, back=False):
            if best_wall is not None and wall > best_wall + bound:
                break  # no newer wall can be an earlier instant
            for at in _instants(wall, zone):
                if at > after and (best is None or at < best):
                    best, best_wall = at, wall
                    bound = _offset_spread(zone, wall)
    except OverflowError:
        pass
    return best


def periodic_slot(
    schedule: CronSchedule,
    zone: tzinfo,
    now: datetime,
    *,
    margin_seconds: int = SLOT_MARGIN_SECONDS,
) -> PeriodicSlot | None:
    """The slot of a run sent at ``now`` (aware): the latest slot of ``schedule``
    (read on the wall clock of ``zone``) at or before it, or ``None`` when the
    schedule has none."""
    moment = now.astimezone(UTC)
    at = _latest(schedule, zone, moment)
    if at is None:
        return None
    following = _following(schedule, zone, at)
    ttl: int | None = _NO_NEXT_SLOT_SECONDS
    if following is not None:
        ttl = int((following - moment).total_seconds()) - margin_seconds
        if ttl < MIN_LEASE_TTL_SECONDS:
            ttl = None
    return PeriodicSlot(at=at, key=at.strftime("%Y%m%dT%H%M%SZ"), ttl_seconds=ttl)


# ---------------------------------------------------------------------- claims


def check_slot_lease_name(lease_name: str) -> None:
    """Raise ``ValueError`` when a slot lease name under ``lease_name`` would
    exceed ``MAX_LEASE_NAME_LENGTH``: a scheduler checks it at start, so a name
    that no store accepts never silently switches the dedupe off."""
    if len(lease_name) + _SLOT_NAME_SUFFIX > MAX_LEASE_NAME_LENGTH:
        longest = MAX_LEASE_NAME_LENGTH - _SLOT_NAME_SUFFIX
        raise ValueError(
            f"the scheduler lease name is {len(lease_name)} characters; the slot "
            f"lease names derived from it need at most {longest} "
            f"(a lease name is up to {MAX_LEASE_NAME_LENGTH} characters)"
        )


def slot_lease_name(
    lease_name: str, periodic_id: str, schedule: CronSchedule, zone: tzinfo
) -> str:
    """The lease name that claims the slots of ``periodic_id`` under
    ``lease_name``: one per id and schedule (a changed cron expression or zone
    starts a new name, so an old claim cannot hold back the new slots). The id
    is hashed: it is the author's string, the name's length is bounded."""
    identity = "\0".join(
        (periodic_id, schedule.expression, getattr(zone, "key", None) or str(zone))
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"{lease_name}:slot:{digest}"


async def claim_periodic_slot(
    store: LeaseStore,
    name: str,
    slot: PeriodicSlot,
    *,
    holder: str,
    fence: int | None,
    limit_seconds: float,
) -> SlotClaim:
    """Claim ``slot`` on ``name`` (:func:`slot_lease_name`) in ``store``.

    The owner label carries the slot and the fence of the scheduler lease its
    holder had (``holder`` names the process), so a diagnostic reading the
    lease sees whose send it was.
    """
    if slot.ttl_seconds is None:
        logger.warning(
            "[periodic] slot %s is too close to the next one to claim; "
            "sending without the dedupe",
            slot.key,
        )
        return SlotClaim("late")
    owner = f"slot:{slot.key}:fence:{fence}:{holder}"[:MAX_LEASE_NAME_LENGTH]
    try:
        lease = await asyncio.wait_for(
            store.acquire(name, ttl_seconds=slot.ttl_seconds, owner=owner),
            timeout=limit_seconds,
        )
    except (Unavailable, Timeout, TimeoutError) as exc:
        logger.warning(
            "[periodic] cannot claim slot %s (%s); sending without the dedupe",
            slot.key,
            type(exc).__name__,
        )
        return SlotClaim("unknown")
    except InfraError as exc:
        logger.error(
            "[periodic] the lease store refused the claim of slot %s (%s: %s); "
            "not sent",
            slot.key,
            type(exc).__name__,
            exc,
        )
        return SlotClaim("refused")
    if lease is None:
        return SlotClaim("taken")
    return SlotClaim("claimed", lease)


__all__ = [
    "SLOT_MARGIN_SECONDS",
    "CronSchedule",
    "PeriodicSlot",
    "SlotClaim",
    "check_slot_lease_name",
    "claim_periodic_slot",
    "periodic_slot",
    "slot_lease_name",
]
