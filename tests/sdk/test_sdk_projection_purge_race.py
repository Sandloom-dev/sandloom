"""A tombstone purge racing an apply never resurrects a deleted row (spec
§6.4 item 5; C-PROJ-01, G-MSTORE-09).

The apply checks the purge watermark, then writes. A purge that raises the
watermark and deletes the tombstone between the two must not let the apply
insert the old version: the check and the apply are atomic with respect to
the purge (the apply's unit holds the watermark row ``FOR SHARE``, the
purge's raise row-locks it). Each test stops the apply exactly between its
check and its write, runs the purge, and only then lets the apply finish.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sdkfix_notes.events import NoteDeleted, NoteSaved
from sdkfix_notes.models import FixtureBase, NoteIndex
from sdkfix_notes.projections import NOTE_INDEX
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import sandloom.sdk._decl.projections as decl
from sandloom.core.drivers.memory_document_store import MemoryDocumentStoreProvider
from sandloom.core.projections.operations import watermark_guarded
from sandloom.core.projections.purge import PurgeFloor, purge_tombstones
from sandloom.core.sql_alchemy.models.projection_watermark import ProjectionWatermark
from sandloom.core.sql_alchemy.projection_watermarks import RelationalWatermarkStore
from sandloom.sdk.projections import Projection
from sandloom.sdk.tenant import bind_tenant

pytestmark = pytest.mark.asyncio

SAVED_GROUP = NOTE_INDEX.groups[0]
#: The smallest purge floor (1 s), so the test can purge "everything so far".
FLOOR = PurgeFloor(stream_retention_s=1.0, relay_horizon_s=0.0)


def _later() -> datetime:
    # the purge runs "an hour from now": its cutoff covers the tombstone and
    # the replayed event, which is then older than the new watermark
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)


Opener = Callable[[], Any]


@pytest.fixture
async def units(db_engine: Any) -> AsyncIterator[Opener]:
    """Committing units on the test database (the race needs two)."""
    async with db_engine.begin() as conn:
        await conn.run_sync(
            FixtureBase.metadata.create_all, tables=[NoteIndex.__table__]
        )
        await conn.run_sync(
            ProjectionWatermark.metadata.create_all,
            tables=[ProjectionWatermark.__table__],
        )
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_unit() -> AsyncIterator[AsyncSession]:
        async with maker() as session, session.begin():
            yield session

    yield open_unit


async def _blocked_by(open_unit: Opener, pid: int) -> bool:
    """Whether some backend waits on a lock ``pid`` holds."""
    async with open_unit() as session:
        found = await session.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE :pid = ANY(pg_blocking_pids(pid))"
            ),
            {"pid": pid},
        )
        return int(found.scalar_one()) > 0


async def _race(
    open_unit: Opener,
    projection: Projection,
    tenant: UUID,
    note: UUID,
    purge_rows: Callable[[datetime], Awaitable[int]],
) -> None:
    """Apply a replayed old NoteSaved, stopped between its watermark check
    and its write while a purge runs; the purge either completes meanwhile
    (no lock: the bug) or waits for the apply (the fix)."""
    (consumer,) = [
        c for c in projection.consumers() if c.source_channel == NoteSaved.channel
    ]
    checked, resume = asyncio.Event(), asyncio.Event()
    inner = consumer.handler

    async def between_check_and_write(session: Any, envelope: Any) -> None:
        checked.set()
        await resume.wait()
        await inner(session, envelope)

    guarded = watermark_guarded(
        dataclasses.replace(consumer, handler=between_check_and_write)
    )
    payload = {
        "note_id": str(note),
        "tenant_id": str(tenant),
        "title": "resurrected",
        "version": 1,
    }
    envelope = {
        "id": f"replay-{note}",
        "payload": payload,
        "emitted_at": datetime.now(UTC) - timedelta(minutes=1),
    }
    pid: list[int] = []

    async def apply() -> None:
        async with open_unit() as session:
            pid.append(
                int(
                    (
                        await session.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                )
            )
            with bind_tenant(tenant):
                await guarded.handler(session, envelope)

    applying = asyncio.create_task(apply())
    await asyncio.wait_for(checked.wait(), 10)
    purging = asyncio.create_task(
        purge_tombstones(
            projection.name,
            older_than=timedelta(seconds=1),
            floor=FLOOR,
            watermarks=RelationalWatermarkStore(open_unit),
            delete_tombstones=purge_rows,
            now=_later,
        )
    )
    # Deterministic: the purge either finishes while the apply is stopped, or
    # is observed waiting on a lock the apply's unit holds.
    for _ in range(1000):
        if purging.done() or await _blocked_by(open_unit, pid[0]):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the purge neither finished nor waited on the apply")
    resume.set()
    await asyncio.wait_for(applying, 10)
    assert await asyncio.wait_for(purging, 10) == 1


async def _cleanup(open_unit: Opener, name: str, tenant: UUID) -> None:
    async with open_unit() as session:
        await session.execute(
            delete(ProjectionWatermark).where(ProjectionWatermark.projection == name)
        )
        with bind_tenant(tenant):
            await session.execute(delete(NoteIndex))


async def test_a_purge_racing_an_apply_never_resurrects_a_relational_row(
    units: Opener,
) -> None:
    projection = Projection(
        f"sdkfix_race.{uuid4().hex[:12]}",
        key=("tenant_id", "note_id"),
        model=NoteIndex,
        groups=[SAVED_GROUP],
    )
    purge = projection.descriptor("sdkfix_notes").purge
    assert purge is not None
    tenant, note = uuid4(), uuid4()
    try:
        async with units() as session:
            with bind_tenant(tenant):
                await projection.apply(
                    session,
                    SAVED_GROUP,
                    NoteDeleted(note_id=note, tenant_id=tenant, version=2),
                    SAVED_GROUP.events[NoteDeleted],
                )

        async def purge_rows(cutoff: datetime) -> int:
            async with units() as session:
                return await purge(session, cutoff)

        await _race(units, projection, tenant, note, purge_rows)
        async with units() as session:
            with bind_tenant(tenant):
                rows = (
                    await session.execute(
                        select(NoteIndex.title, NoteIndex.deleted_at).where(
                            NoteIndex.note_id == note
                        )
                    )
                ).all()
        assert all(deleted is not None for _title, deleted in rows), rows
    finally:
        await _cleanup(units, projection.name, tenant)


async def test_a_purge_racing_an_apply_never_resurrects_a_store_row(
    units: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = MemoryDocumentStoreProvider(name="mem", driver="memory", options={})
    monkeypatch.setattr(decl, "capability", lambda kind, name="default": provider)
    projection = Projection(
        f"sdkfix_race.{uuid4().hex[:12]}",
        key=("tenant_id", "note_id"),
        store=("document_store", "mem"),
        groups=[SAVED_GROUP],
    )
    purge = projection.descriptor("sdkfix").purge
    assert purge is not None
    tenant, note = uuid4(), uuid4()
    try:
        with bind_tenant(tenant):
            await projection.apply(
                None,
                SAVED_GROUP,
                NoteDeleted(note_id=note, tenant_id=tenant, version=2),
                SAVED_GROUP.events[NoteDeleted],
            )

        async def purge_rows(cutoff: datetime) -> int:
            return await purge(None, cutoff)

        await _race(units, projection, tenant, note, purge_rows)
        store: Any = projection.target()
        assert await store.get(tenant_id=tenant, key={"note_id": note}) is None
        assert (
            await store.version_of(
                tenant_id=tenant,
                key={"tenant_id": tenant, "note_id": note},
                version_column="note_version",
            )
            is None
        )
    finally:
        await _cleanup(units, projection.name, tenant)
