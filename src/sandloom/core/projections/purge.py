"""Tombstone purge, the purge watermark and the rebuild guard (spec §6.4
item 5).

Tombstones must outlive the whole replay and rebuild horizon, which stream
retention does NOT bound: ``sandloom outbox requeue`` and ``sandloom streams
dlq-replay`` can redeliver arbitrarily old events. So tombstones are purged
only by :func:`purge_tombstones`, which

(a) refuses an ``older_than`` below the purge floor — the larger of stream
    retention + the relay's redelivery horizon, the age of the oldest DLQ
    entry of a source channel and the age of the oldest dead outbox row of
    a source channel (:func:`purge_floor_seconds`);
(b) refuses while a rebuild of the projection is in progress;
(c) raises the projection's ``purged_before`` watermark BEFORE deleting
    (monotonic: never lowered, rebuilds included), then deletes the
    tombstones older than it.

(b) and (c) run inside the store's purge window
(:meth:`WatermarkStore.purge_window`), which a rebuild's entry
(``begin_rebuild``) excludes: a rebuild that starts after the purge read
"no rebuild" waits until the raise and the delete are done, and a purge
after a begun rebuild sees it. The check is never a stale read.

From then on the apply path refuses an event emitted before the watermark
(:func:`check_watermark`): it is dead-lettered with "older than the tombstone
purge watermark", so an operator replay can never resurrect a purged row.
The apply's check and its write are atomic with respect to (c): the apply
holds the watermark under a share lock until it commits and the raise takes
it exclusively, so a purge waits for an apply that already passed the check
(the relational store: ``locked_watermark``).

Store-agnostic: the watermark lives in a :class:`WatermarkStore` (the
relational one, ``projection_watermarks`` in the write store, is
:mod:`sandloom.core.sql_alchemy.projection_watermarks`) and the delete is the
caller's (the projection knows its target).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sandloom.core.eventing.delivery import PoisonEventError

#: The dead-letter reason of an event older than the watermark.
OLDER_THAN_WATERMARK = "older than the tombstone purge watermark"


@dataclass(frozen=True)
class Watermark:
    """A projection's purge state: the watermark, the start of the oldest
    running rebuild (``None``: none runs) and how many rebuilds run."""

    purged_before: datetime | None = None
    rebuild_started_at: datetime | None = None
    rebuilds: int = 0


class WatermarkLike(Protocol):
    """What a store returns for a projection (``Watermark`` or the
    relational store's row)."""

    @property
    def purged_before(self) -> datetime | None: ...

    @property
    def rebuild_started_at(self) -> datetime | None: ...


class WatermarkStore(Protocol):
    """Where a projection's watermark lives (its write store)."""

    async def get(self, projection: str) -> WatermarkLike: ...

    async def raise_purged_before(self, projection: str, before: datetime) -> datetime:
        """Set ``purged_before = max(current, before)``; returns the result."""
        ...

    def purge_window(
        self, projection: str
    ) -> AbstractAsyncContextManager[WatermarkLike]:
        """Exclude :meth:`begin_rebuild` of ``projection`` for the block and
        yield its mark read inside the exclusion (a purge checks the rebuild
        marker, raises the watermark and deletes in the block)."""
        ...

    async def begin_rebuild(self, projection: str, at: datetime) -> None:
        """Count one more running rebuild (``rebuild_started_at`` is set
        while any runs: the purge exclusion is per rebuild, never one flag
        the first finishing rebuild clears for another). Waits while a
        :meth:`purge_window` of ``projection`` is open."""
        ...

    async def end_rebuild(self, projection: str) -> None:
        """Count one rebuild less (never below zero); the marker clears only
        when none runs any more."""
        ...


class PurgeRefused(RuntimeError):
    """A tombstone purge the replay horizon or a running rebuild forbids."""


@dataclass(frozen=True)
class PurgeFloor:
    """The inputs of the purge floor (seconds)."""

    stream_retention_s: float
    relay_horizon_s: float
    oldest_dlq_age_s: float = 0.0
    oldest_dead_outbox_age_s: float = 0.0


def purge_floor_seconds(floor: PurgeFloor) -> float:
    """The smallest ``older_than`` a purge may use (``inf``: never — a stream
    without a time-bounded retention keeps every event replayable)."""
    if floor.stream_retention_s <= 0:
        return float("inf")
    return max(
        floor.stream_retention_s + floor.relay_horizon_s,
        floor.oldest_dlq_age_s,
        floor.oldest_dead_outbox_age_s,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def purge_tombstones(
    projection: str,
    *,
    older_than: timedelta,
    floor: PurgeFloor,
    watermarks: WatermarkStore,
    delete_tombstones: Callable[[datetime], Awaitable[int]],
    now: Callable[[], datetime] = _now,
) -> int:
    """Purge ``projection``'s tombstones older than ``older_than`` (module
    docstring); returns how many rows ``delete_tombstones(cutoff)`` removed.
    Raises :class:`PurgeRefused` below the floor or during a rebuild."""
    minimum = purge_floor_seconds(floor)
    if older_than.total_seconds() < minimum:
        raise PurgeRefused(
            f"purge_tombstones({projection!r}, older_than={older_than}) is below "
            f"the replay horizon ({minimum:.0f} s: stream retention + relay "
            "horizon, the oldest DLQ entry and the oldest dead outbox row of the "
            "source channels): an operator replay could resurrect a purged row"
        )
    # no rebuild enters between the check and the delete (G-MSTORE-09)
    async with watermarks.purge_window(projection) as mark:
        if mark.rebuild_started_at is not None:
            raise PurgeRefused(
                f"a rebuild of {projection!r} is in progress (since "
                f"{mark.rebuild_started_at.isoformat()}): purge after it finishes"
            )
        cutoff = now() - older_than
        # the watermark first: a replay racing the delete is already refused
        await watermarks.raise_purged_before(projection, cutoff)
        return await delete_tombstones(cutoff)


def _emitted_at(envelope: Mapping[str, Any]) -> datetime | None:
    raw = envelope.get("emitted_at")
    if isinstance(raw, datetime):
        stamp = raw
    elif isinstance(raw, str):
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(UTC).replace(tzinfo=None)
    return stamp


def check_watermark(mark: WatermarkLike, envelope: Mapping[str, Any]) -> None:
    """Refuse (dead-letter, :class:`PoisonEventError`) an event emitted before
    the projection's purge watermark. An event without a readable
    ``emitted_at`` is refused too once a watermark exists (fail closed)."""
    before = mark.purged_before
    if before is None:
        return
    emitted = _emitted_at(envelope)
    if emitted is None or emitted < before:
        raise PoisonEventError(
            f"event {envelope.get('id')!r} is {OLDER_THAN_WATERMARK} "
            f"({before.isoformat()})"
        )


@asynccontextmanager
async def rebuilding(
    projection: str,
    watermarks: WatermarkStore,
    *,
    now: Callable[[], datetime] = _now,
) -> AsyncIterator[None]:
    """Mark a rebuild in progress for its duration (purges refuse meanwhile).
    The watermark is never lowered: a rebuild keeps refusing events older
    than it, so an old creation event cannot resurrect a purged row."""
    await watermarks.begin_rebuild(projection, now())
    try:
        yield
    finally:
        await watermarks.end_rebuild(projection)


class MemoryWatermarkStore:
    """An in-process :class:`WatermarkStore` (tests, the in-memory store)."""

    def __init__(self) -> None:
        self._marks: dict[str, Watermark] = {}
        self._windows: dict[str, asyncio.Lock] = {}

    def _window(self, projection: str) -> asyncio.Lock:
        return self._windows.setdefault(projection, asyncio.Lock())

    async def get(self, projection: str) -> Watermark:
        return self._marks.get(projection, Watermark())

    @asynccontextmanager
    async def purge_window(self, projection: str) -> AsyncIterator[Watermark]:
        async with self._window(projection):
            yield await self.get(projection)

    async def raise_purged_before(self, projection: str, before: datetime) -> datetime:
        mark = self._marks.get(projection, Watermark())
        current = mark.purged_before
        new = before if current is None or before > current else current
        self._marks[projection] = Watermark(new, mark.rebuild_started_at, mark.rebuilds)
        return new

    async def begin_rebuild(self, projection: str, at: datetime) -> None:
        async with self._window(projection):
            mark = self._marks.get(projection, Watermark())
            started = mark.rebuild_started_at if mark.rebuilds else at
            self._marks[projection] = Watermark(
                mark.purged_before, started, mark.rebuilds + 1
            )

    async def end_rebuild(self, projection: str) -> None:
        mark = self._marks.get(projection, Watermark())
        left = max(mark.rebuilds - 1, 0)
        self._marks[projection] = Watermark(
            mark.purged_before, mark.rebuild_started_at if left else None, left
        )


__all__ = [
    "OLDER_THAN_WATERMARK",
    "MemoryWatermarkStore",
    "PurgeFloor",
    "PurgeRefused",
    "Watermark",
    "WatermarkLike",
    "WatermarkStore",
    "check_watermark",
    "purge_floor_seconds",
    "purge_tombstones",
    "rebuilding",
]
