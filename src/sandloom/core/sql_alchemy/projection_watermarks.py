"""The relational ``sandloom.core.projections.purge.WatermarkStore``: the
``projection_watermarks`` core table in the projection's write store.

Every write is one statement in its own short unit (``open``): the watermark
must commit BEFORE the tombstones are deleted, and a rebuild marker must be
visible to a concurrent purge. The marker counts running rebuilds
(``rebuilds_active``, row-locked upserts): ``rebuild_started_at`` stays set
until the LAST running rebuild ends, so one rebuild finishing never lifts the
purge exclusion of another (G-MSTORE-09). ``raise_purged_before`` is a monotonic upsert
(``GREATEST``), so two purges racing never lower it.

A purge runs inside :meth:`RelationalWatermarkStore.purge_window`: a unit
holding the projection's transaction-level advisory lock
(:func:`purge_lock_key`) while the purge reads the rebuild marker, raises the
watermark and deletes (each still its own committed unit). ``begin_rebuild``
takes the same lock before counting itself, so a rebuild that starts after
the purge read "no rebuild" waits until the delete is done, and the purge
reads the marker only after every begun rebuild committed (G-MSTORE-09). A
purge that dies releases the lock with its connection.

The apply path reads the row with :func:`locked_watermark` (``FOR SHARE``, in
the consumer's unit, the row created first when missing): the lock lasts
until the apply commits, and ``raise_purged_before``'s upsert row-locks the
same row (``FOR UPDATE``), so a purge waits for every apply that already
passed the check, and an apply that checks after the raise sees the new
watermark. The check and the apply are atomic with respect to the purge: an
apply can never write after the tombstones it was checked against are gone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

from sqlalchemy import Table, case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.models.projection_watermark import ProjectionWatermark

_TABLE = cast(Table, ProjectionWatermark.__table__)
_OUTBOX = cast(Table, OutboxMessage.__table__)


class StoredWatermark(NamedTuple):
    """A projection's row (``purge.WatermarkLike``)."""

    purged_before: datetime | None = None
    rebuild_started_at: datetime | None = None


#: Purge window vs rebuild entry, per projection (transaction scoped).
_PURGE_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def purge_lock_key(projection: str) -> str:
    """The text whose 64-bit hash keys ``projection``'s purge lock."""
    return f"sandloom.projection_purge:{projection}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def stored_watermark(session: Any, projection: str) -> StoredWatermark:
    """The projection's row read in ``session`` (the consumer's own unit:
    the event handler checks it before applying)."""
    row = (
        await session.execute(
            select(_TABLE.c.purged_before, _TABLE.c.rebuild_started_at).where(
                _TABLE.c.projection == projection
            )
        )
    ).first()
    return StoredWatermark() if row is None else StoredWatermark(row[0], row[1])


async def locked_watermark(session: Any, projection: str) -> StoredWatermark:
    """The projection's row read in ``session`` (the consumer's unit) under
    ``FOR SHARE`` until that unit ends: a purge's watermark raise waits for
    the apply. A missing row is created first (``ON CONFLICT DO NOTHING``,
    once per projection), so even the first purge has a row to wait on."""
    stmt = (
        select(_TABLE.c.purged_before, _TABLE.c.rebuild_started_at)
        .where(_TABLE.c.projection == projection)
        .with_for_update(read=True)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        await session.execute(
            pg_insert(_TABLE)
            .values(projection=projection, updated_at=_now())
            .on_conflict_do_nothing(index_elements=["projection"])
        )
        row = (await session.execute(stmt)).first()
    return StoredWatermark() if row is None else StoredWatermark(row[0], row[1])


async def oldest_dead_outbox_age_s(
    session: Any, channels: Iterable[str], now: datetime | None = None
) -> float:
    """Age in seconds of the oldest dead-lettered outbox row on ``channels``
    (0 when none): a replay of it may still deliver a tombstoned key."""
    names = list(channels)
    if not names:
        return 0.0
    oldest = (
        await session.execute(
            select(func.min(_OUTBOX.c.emitted_at)).where(
                _OUTBOX.c.dead_at.is_not(None), _OUTBOX.c.channel.in_(names)
            )
        )
    ).scalar()
    if oldest is None:
        return 0.0
    return max(0.0, float(((now or _now()) - oldest).total_seconds()))


class RelationalWatermarkStore:
    """``open()`` yields a session whose block commits (``db_session(name)``)."""

    def __init__(self, open: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._open = open

    async def get(self, projection: str) -> StoredWatermark:
        async with self._open() as session:
            return await stored_watermark(session, projection)

    @asynccontextmanager
    async def purge_window(self, projection: str) -> AsyncIterator[StoredWatermark]:
        async with self._open() as session:
            await session.execute(_PURGE_LOCK, {"key": purge_lock_key(projection)})
            # read after the lock: every begun rebuild has committed its count
            yield await stored_watermark(session, projection)

    async def raise_purged_before(self, projection: str, before: datetime) -> datetime:
        insert = pg_insert(_TABLE).values(
            projection=projection, purged_before=before, updated_at=_now()
        )
        stmt = insert.on_conflict_do_update(
            index_elements=["projection"],
            set_={
                "purged_before": func.greatest(
                    func.coalesce(
                        _TABLE.c.purged_before, insert.excluded.purged_before
                    ),
                    insert.excluded.purged_before,
                ),
                "updated_at": insert.excluded.updated_at,
            },
        ).returning(_TABLE.c.purged_before)
        async with self._open() as session:
            result = (await session.execute(stmt)).scalar_one()
        return result  # type: ignore[no-any-return]

    async def begin_rebuild(self, projection: str, at: datetime) -> None:
        stmt = pg_insert(_TABLE).values(
            projection=projection,
            rebuild_started_at=at,
            rebuilds_active=1,
            updated_at=_now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["projection"],
            set_={
                # the oldest running rebuild's start (kept while one runs)
                "rebuild_started_at": case(
                    (_TABLE.c.rebuilds_active > 0, _TABLE.c.rebuild_started_at),
                    else_=stmt.excluded.rebuild_started_at,
                ),
                "rebuilds_active": _TABLE.c.rebuilds_active + 1,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        async with self._open() as session:
            # wait for a purge window: a purge never deletes under a rebuild
            await session.execute(_PURGE_LOCK, {"key": purge_lock_key(projection)})
            await session.execute(stmt)

    async def end_rebuild(self, projection: str) -> None:
        # SET reads the row's values before the update: the marker clears
        # only when this was the last running rebuild
        async with self._open() as session:
            await session.execute(
                update(_TABLE)
                .where(_TABLE.c.projection == projection)
                .values(
                    rebuilds_active=func.greatest(_TABLE.c.rebuilds_active - 1, 0),
                    rebuild_started_at=case(
                        (_TABLE.c.rebuilds_active > 1, _TABLE.c.rebuild_started_at),
                        else_=None,
                    ),
                    updated_at=_now(),
                )
            )


__all__ = [
    "RelationalWatermarkStore",
    "StoredWatermark",
    "locked_watermark",
    "oldest_dead_outbox_age_s",
    "purge_lock_key",
    "stored_watermark",
]
