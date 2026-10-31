"""Projections to any store and the projection operations (spec §6.4 items
4, 5 and 7; ledger G-PROJ-14..17, C-PROJ-07/08).

A ``store=`` target applies through the version-guarded conditional writes
and is read by ``View``; the tombstone purge and the watermark guard on the
apply path; ``rebuild_all`` / ``rebuild_entity``; consistency tokens: the
entries a write's events produce, a read that waits for them and a read that
serves stale with ``SANDLOOM-Projection-Stale`` once its budget is spent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict
from sdkfix_notes.events import NoteDeleted, NoteLiked, NoteSaved
from sdkfix_notes.models import FixtureBase, NoteIndex
from sdkfix_notes.projections import NOTE_INDEX
from sqlalchemy import select

import sandloom.core.http.route_runtime as route_runtime
import sandloom.core.projections.tokens as tokens_module
import sandloom.sdk._decl.projections as decl
from sandloom.core.drivers.memory_document_store import MemoryDocumentStoreProvider
from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.facade import CoreFacade
from sandloom.core.projections.operations import response_token, watermark_guarded
from sandloom.core.projections.tokens import (
    aggregate_digest,
    issue_token,
    key_digest,
    read_token,
)
from sandloom.core.sql_alchemy.models.projection_watermark import ProjectionWatermark
from sandloom.core.unit_of_work.scope import _OUTBOX_PENDING_KEY, _PendingEvent
from sandloom.sdk.errors import ProjectionDefinitionError
from sandloom.sdk.projections import (
    ConsistencyToken,
    Projection,
    ProjectionView,
    SourceGroup,
    delta,
    full_state,
    tombstone,
)
from sandloom.sdk.tenant import bind_tenant

KEYS = {"k1": "s" * 32}
SAVED_GROUP = NOTE_INDEX.groups[0]


class NoteRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    note_id: UUID
    title: str | None


def _store_projection(**overrides: Any) -> Projection:
    args: dict[str, Any] = {
        "key": ("tenant_id", "note_id"),
        "store": ("document_store", "mem"),
        "groups": [
            SourceGroup(
                events={
                    NoteSaved: full_state(lambda e: {"title": e.title}),
                    NoteDeleted: tombstone(lambda e: {}),
                },
                aggregate_key="note_id",
                version_field="version",
                version_column="note_version",
                owns=("title",),
            )
        ],
    }
    args.update(overrides)
    return Projection("sdkfix.store_index", **args)


@pytest.fixture
def memory_store(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStoreProvider:
    provider = MemoryDocumentStoreProvider(name="mem", driver="memory", options={})

    def resolve(ref: Any, name: str = "default") -> Any:
        assert (ref.name, name) == ("document_store", "mem")
        return provider

    monkeypatch.setattr(decl, "capability", resolve)
    return provider


# -- store targets -------------------------------------------------------------


async def test_a_store_projection_converges_and_reads_skip_tombstones(
    memory_store: MemoryDocumentStoreProvider,
) -> None:
    projection = _store_projection()
    group = projection.groups[0]
    tenant, kept, gone = uuid4(), uuid4(), uuid4()
    saved, deleted = group.events[NoteSaved], group.events[NoteDeleted]
    with bind_tenant(tenant):
        for version in (2, 1, 3, 2):  # duplicates and reordering
            event = NoteSaved(
                note_id=kept, tenant_id=tenant, title=f"v{version}", version=version
            )
            await projection.apply(None, group, event, saved)
        await projection.apply(
            None, group, NoteDeleted(note_id=gone, tenant_id=tenant, version=5), deleted
        )
        late = NoteSaved(note_id=gone, tenant_id=tenant, title="late", version=4)
        assert await projection.apply(None, group, late, saved) is False
        view: ProjectionView[NoteRow] = ProjectionView(projection, NoteRow, None)
        assert await view.get({"note_id": kept}) == NoteRow(note_id=kept, title="v3")
        assert await view.get({"note_id": gone}) is None
        page = await view.query(limit=10)
    assert [r.note_id for r in page.items] == [kept]


async def test_an_independent_group_never_resurrects_a_store_tombstone(
    memory_store: MemoryDocumentStoreProvider,
) -> None:
    """As on a relational model: only the group that owns deletion clears a
    tombstone, with a newer version; another group's upsert keeps it."""
    title = SourceGroup(
        events={
            NoteSaved: full_state(lambda e: {"title": e.title}),
            NoteDeleted: tombstone(lambda e: {}),
        },
        aggregate_key="note_id",
        version_field="version",
        version_column="title_version",
        owns=("title",),
    )
    likes = SourceGroup(
        events={NoteLiked: full_state(lambda e: {"likes": 1})},
        aggregate_key="note_id",
        version_field="version",
        version_column="like_version",
        owns=("likes",),
    )
    projection = _store_projection(groups=[title, likes])
    tenant, note = uuid4(), uuid4()
    with bind_tenant(tenant):
        await projection.apply(
            None,
            title,
            NoteSaved(note_id=note, tenant_id=tenant, title="t", version=1),
            title.events[NoteSaved],
        )
        await projection.apply(
            None,
            title,
            NoteDeleted(note_id=note, tenant_id=tenant, version=2),
            title.events[NoteDeleted],
        )
        assert await projection.apply(
            None,
            likes,
            NoteLiked(note_id=note, tenant_id=tenant, version=1),
            likes.events[NoteLiked],
        )
        view: ProjectionView[NoteRow] = ProjectionView(projection, NoteRow, None)
        assert await view.get({"note_id": note}) is None
        assert (await view.query()).items == []
        await projection.apply(
            None,
            title,
            NoteSaved(note_id=note, tenant_id=tenant, title="back", version=3),
            title.events[NoteSaved],
        )
        assert await view.get({"note_id": note}) == NoteRow(note_id=note, title="back")


def test_a_store_target_refuses_deltas_and_two_targets() -> None:
    liked = SourceGroup(
        events={NoteLiked: delta(lambda e: {"likes": 1})},
        aggregate_key="note_id",
        version_field="version",
        version_column="like_version",
        owns=("likes",),
    )
    with pytest.raises(ProjectionDefinitionError, match="store target"):
        _store_projection(groups=[liked])
    with pytest.raises(ProjectionDefinitionError, match="two targets"):
        _store_projection(model=NoteIndex)
    with pytest.raises(ProjectionDefinitionError, match="capability, instance"):
        _store_projection(store=("document_store",))


async def test_a_store_row_of_another_tenant_is_refused_and_reads_are_per_tenant(
    memory_store: MemoryDocumentStoreProvider,
) -> None:
    projection = _store_projection()
    group = projection.groups[0]
    saved = group.events[NoteSaved]
    mine, other, note = uuid4(), uuid4(), uuid4()
    foreign = NoteSaved(note_id=note, tenant_id=other, title="x", version=1)
    with bind_tenant(mine), pytest.raises(ProjectionDefinitionError, match="tenant"):
        await projection.apply(None, group, foreign, saved)
    with bind_tenant(other):
        await projection.apply(None, group, foreign, saved)
    with bind_tenant(mine):
        view: ProjectionView[NoteRow] = ProjectionView(projection, NoteRow, None)
        assert await view.get({"note_id": note}) is None
        assert (await view.query()).items == []
        # A key naming another tenant never overrides the bound one.
        assert await view.get({"tenant_id": other, "note_id": note}) is None
        assert (await view.query(filter={"tenant_id": other})).items == []
    with bind_tenant(other):
        own: ProjectionView[NoteRow] = ProjectionView(projection, NoteRow, None)
        assert await own.get({"tenant_id": other, "note_id": note}) is not None
    unbound: ProjectionView[NoteRow] = ProjectionView(projection, NoteRow, None)
    with pytest.raises(LookupError, match="bound tenant"):
        await unbound.get({"tenant_id": other, "note_id": note})


async def test_a_blue_green_rebuild_runs_in_place_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandloom.core.projections.operations as ops
    from sandloom.core.projections import (
        ProjectionRebuildResult,
        QueryProjectionDescriptor,
    )
    from sandloom.core.projections.purge import MemoryWatermarkStore

    class _Rebuilder:
        async def rebuild_all(self) -> ProjectionRebuildResult:
            mark = await marks.get("p")
            assert mark.rebuild_started_at is not None  # purges refuse meanwhile
            return ProjectionRebuildResult(projection="p", rows=2)

    marks = MemoryWatermarkStore()
    monkeypatch.setattr(ops, "watermark_store", lambda _storage: marks)
    core = CoreFacade(app=FastAPI())
    core.register_query_projection(
        QueryProjectionDescriptor(name="p", owner_module="m")
    )
    core.register_projection_rebuilder("p", _Rebuilder())
    result = await ops.rebuild_projection("p", blue_green=True, core=core)
    assert (result.rows, result.details["blue_green"]) == (2, "in_place")
    assert (await marks.get("p")).rebuild_started_at is None


async def test_a_store_without_conditional_writes_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decl, "capability", lambda kind, name="default": object())
    projection = _store_projection()
    group = projection.groups[0]
    tenant = uuid4()
    event = NoteSaved(note_id=uuid4(), tenant_id=tenant, title="t", version=1)
    with (
        bind_tenant(tenant),
        pytest.raises(ProjectionDefinitionError, match="conditional_write"),
    ):
        await projection.apply(None, group, event, group.events[NoteSaved])


async def test_a_store_purge_deletes_old_tombstones_only(
    memory_store: MemoryDocumentStoreProvider,
) -> None:
    projection = _store_projection()
    group = projection.groups[0]
    tenant = uuid4()
    with bind_tenant(tenant):
        await projection.apply(
            None,
            group,
            NoteDeleted(note_id=uuid4(), tenant_id=tenant, version=1),
            group.events[NoteDeleted],
        )
    purge = projection.descriptor("sdkfix").purge
    assert purge is not None
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    assert await purge(None, past) == 0
    assert await purge(None, past + timedelta(days=2)) == 1


# -- relational purge, watermark guard ------------------------------------------


@pytest_asyncio.fixture
async def index_db(db: Any) -> AsyncIterator[Any]:
    def create(sync: Any) -> None:
        FixtureBase.metadata.create_all(sync.connection(), tables=[NoteIndex.__table__])

    await db.run_sync(create)
    yield db


async def test_the_relational_purge_deletes_tombstones_older_than_the_cutoff(
    index_db: Any,
) -> None:
    tenant, gone, kept = uuid4(), uuid4(), uuid4()
    with bind_tenant(tenant):
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteDeleted(note_id=gone, tenant_id=tenant, version=2),
            SAVED_GROUP.events[NoteDeleted],
        )
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteSaved(note_id=kept, tenant_id=tenant, title="t", version=1),
            SAVED_GROUP.events[NoteSaved],
        )
    purge = NOTE_INDEX.descriptor("sdkfix_notes").purge
    assert purge is not None
    now = datetime.now(UTC).replace(tzinfo=None)
    assert await purge(index_db, now - timedelta(hours=1)) == 0
    assert await purge(index_db, now + timedelta(hours=1)) == 1
    index_db.expire_all()
    with bind_tenant(tenant):
        left = (await index_db.execute(select(NoteIndex.note_id))).scalars().all()
    assert set(left) == {kept}


async def test_an_event_older_than_the_watermark_is_refused_on_apply(
    index_db: Any,
) -> None:
    consumer = watermark_guarded(NOTE_INDEX.consumers()[0])
    mark = datetime.now(UTC).replace(tzinfo=None)
    index_db.add(
        ProjectionWatermark(
            projection=NOTE_INDEX.name, purged_before=mark, updated_at=mark
        )
    )
    await index_db.flush()
    tenant, note = uuid4(), uuid4()
    payload = {
        "note_id": str(note),
        "tenant_id": str(tenant),
        "title": "t",
        "version": 1,
    }
    old = {"id": "e-old", "payload": payload, "emitted_at": mark - timedelta(seconds=1)}
    with bind_tenant(tenant), pytest.raises(PoisonEventError, match="watermark"):
        await consumer.handler(index_db, old)
    new = {"id": "e-new", "payload": payload, "emitted_at": mark + timedelta(seconds=1)}
    with bind_tenant(tenant):
        await consumer.handler(index_db, new)
        index_db.expire_all()
        assert (await index_db.get(NoteIndex, (tenant, note))) is not None


def test_only_a_projection_with_tombstones_gets_the_guard() -> None:
    core = CoreFacade(app=FastAPI())
    core.register_query_projection(NOTE_INDEX.descriptor("sdkfix_notes"))
    for consumer in NOTE_INDEX.consumers():
        core.register_projection_consumer(consumer)
    wrapped = core.projection_registry.consumers_for(NOTE_INDEX.name)
    assert all(hasattr(c.handler, "__wrapped__") for c in wrapped)

    no_tombstones = _store_projection(
        groups=[
            SourceGroup(
                events={NoteSaved: full_state(lambda e: {"title": e.title})},
                aggregate_key="note_id",
                version_field="version",
                version_column="note_version",
                owns=("title",),
            )
        ]
    )
    other = CoreFacade(app=FastAPI())
    other.register_query_projection(no_tombstones.descriptor("sdkfix"))
    (consumer,) = no_tombstones.consumers()
    other.register_projection_consumer(consumer)
    (registered,) = other.projection_registry.consumers_for(no_tombstones.name)
    assert registered.handler is consumer.handler


# -- rebuilds --------------------------------------------------------------------


async def test_rebuild_entity_applies_one_aggregate_and_checks_its_ref(
    index_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, one, two = uuid4(), uuid4(), uuid4()

    async def list_for_tenant(tenant_id: UUID) -> list[Any]:
        return [
            NoteSaved(note_id=one, tenant_id=tenant_id, title="one", version=1),
            NoteSaved(note_id=two, tenant_id=tenant_id, title="two", version=1),
        ]

    @asynccontextmanager
    async def same_session() -> AsyncIterator[Any]:
        yield index_db

    monkeypatch.setattr(decl, "open_uow", same_session)
    projection = Projection(
        "sdkfix.rebuildable",
        key=NOTE_INDEX.key,
        model=NoteIndex,
        groups=[SAVED_GROUP],
        rebuild_from=list_for_tenant,
    )
    rebuilder = projection.rebuilder()
    assert rebuilder is not None
    with pytest.raises(ProjectionDefinitionError, match="tenant id"):
        await rebuilder.rebuild_entity("not-a-ref")
    result = await rebuilder.rebuild_entity(f"{tenant}:{one}")
    assert result.rows == 1
    with bind_tenant(tenant):
        index_db.expire_all()
        assert await index_db.get(NoteIndex, (tenant, one)) is not None
        assert await index_db.get(NoteIndex, (tenant, two)) is None
    everything = await rebuilder.rebuild_all()
    assert everything.details["tenants"] >= 1
    with bind_tenant(tenant):
        index_db.expire_all()
        assert await index_db.get(NoteIndex, (tenant, two)) is not None


# -- consistency tokens ------------------------------------------------------------


def test_a_write_token_names_the_rows_its_events_produce() -> None:
    tenant, note = uuid4(), uuid4()
    payload = {
        "note_id": str(note),
        "tenant_id": str(tenant),
        "title": "t",
        "version": 4,
    }
    descriptor = NOTE_INDEX.descriptor("sdkfix_notes")
    entries = list(descriptor.token_entries(NoteSaved.channel, payload))
    assert [(dict(k), g, a, v) for k, g, a, v in entries] == [
        ({"tenant_id": tenant, "note_id": note}, "note_version", note, 4)
    ]

    class _Session:
        info = {
            _OUTBOX_PENDING_KEY: [
                _PendingEvent(None, NoteSaved.channel, "v1", payload),  # type: ignore[arg-type]
                _PendingEvent(None, "other.channel.v1", "v1", {}),  # type: ignore[arg-type]
            ]
        }

    sources = {NoteSaved.channel: ((NOTE_INDEX.name, descriptor.token_entries),)}
    with bind_tenant(tenant):
        assert response_token(sources, [_Session()]) is None  # no signing key
    with bind_tenant(tenant), pytest.MonkeyPatch.context() as mp:
        mp.setattr(tokens_module, "_keys", lambda keys: KEYS)
        header = response_token(sources, [_Session()])
        token = read_token(header, tenant_id=tenant, keys=KEYS)
    assert token is not None
    assert token.versions(NOTE_INDEX.name) == {
        (
            key_digest({"tenant_id": tenant, "note_id": note}),
            "note_version",
            aggregate_digest(note),
        ): 4
    }


async def test_a_read_waits_for_the_token_then_serves_stale(index_db: Any) -> None:
    tenant, note = uuid4(), uuid4()
    key = {"tenant_id": tenant, "note_id": note}
    header = issue_token(
        [(NOTE_INDEX.name, key, "note_version", note, 2)], tenant_id=tenant, keys=KEYS
    )
    token = read_token(header, tenant_id=tenant, keys=KEYS)
    assert isinstance(token, ConsistencyToken)
    saved = SAVED_GROUP.events[NoteSaved]
    with bind_tenant(tenant):
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteSaved(note_id=note, tenant_id=tenant, title="v1", version=1),
            saved,
        )
        response = Response()
        view: ProjectionView[NoteRow] = ProjectionView(
            NOTE_INDEX, NoteRow, index_db, wait_ms=20, token=token, response=response
        )
        assert await view.get({"note_id": note}) == NoteRow(note_id=note, title="v1")
        assert view.stale is True
        assert response.headers["SANDLOOM-Projection-Stale"] == "true"

        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteSaved(note_id=note, tenant_id=tenant, title="v2", version=2),
            saved,
        )
        fresh: ProjectionView[NoteRow] = ProjectionView(
            NOTE_INDEX, NoteRow, index_db, wait_ms=20, token=token, response=Response()
        )
        assert await fresh.get({"note_id": note}) == NoteRow(note_id=note, title="v2")
        page = await fresh.query(filter={"tenant_id": tenant})
        assert [r.title for r in page.items] == ["v2"]
        assert fresh.stale is False


async def test_a_token_of_one_group_is_not_satisfied_by_another_groups_version(
    index_db: Any,
) -> None:
    """The reviewed example: title_version=1, like_version=100 does not
    satisfy a token expecting title version 5 — the read waits, then serves
    the old title with stale=True (never stale=False); a relational get and
    query alike, and a store target alike."""
    tenant, note = uuid4(), uuid4()
    key = {"tenant_id": tenant, "note_id": note}
    header = issue_token(
        [(NOTE_INDEX.name, key, "note_version", note, 5)], tenant_id=tenant, keys=KEYS
    )
    token = read_token(header, tenant_id=tenant, keys=KEYS)
    assert isinstance(token, ConsistencyToken)
    liked_group = NOTE_INDEX.groups[1]
    with bind_tenant(tenant):
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteSaved(note_id=note, tenant_id=tenant, title="old", version=1),
            SAVED_GROUP.events[NoteSaved],
        )
        for version in range(1, 101):
            await NOTE_INDEX.apply(
                index_db,
                liked_group,
                NoteLiked(note_id=note, tenant_id=tenant, version=version),
                liked_group.events[NoteLiked],
            )
        index_db.expire_all()
        row = await index_db.get(NoteIndex, (tenant, note))
        assert (row.note_version, row.like_version) == (1, 100)
        view: ProjectionView[NoteRow] = ProjectionView(
            NOTE_INDEX, NoteRow, index_db, wait_ms=20, token=token, response=Response()
        )
        assert await view.get({"note_id": note}) == NoteRow(note_id=note, title="old")
        assert view.stale is True
        paged: ProjectionView[NoteRow] = ProjectionView(
            NOTE_INDEX, NoteRow, index_db, wait_ms=20, token=token, response=Response()
        )
        page = await paged.query(filter={"tenant_id": tenant})
        assert [r.title for r in page.items] == ["old"]
        assert paged.stale is True


async def test_a_store_token_compares_its_own_groups_version(
    memory_store: MemoryDocumentStoreProvider,
) -> None:
    title = SourceGroup(
        events={NoteSaved: full_state(lambda e: {"title": e.title})},
        aggregate_key="note_id",
        version_field="version",
        version_column="title_version",
        owns=("title",),
    )
    likes = SourceGroup(
        events={NoteDeleted: full_state(lambda e: {"likes": 1})},
        aggregate_key="note_id",
        version_field="version",
        version_column="like_version",
        owns=("likes",),
    )
    projection = _store_projection(groups=[title, likes])
    tenant, note = uuid4(), uuid4()
    key = {"tenant_id": tenant, "note_id": note}
    header = issue_token(
        [(projection.name, key, "title_version", note, 5)], tenant_id=tenant, keys=KEYS
    )
    token = read_token(header, tenant_id=tenant, keys=KEYS)
    assert isinstance(token, ConsistencyToken)
    with bind_tenant(tenant):
        await projection.apply(
            None,
            title,
            NoteSaved(note_id=note, tenant_id=tenant, title="old", version=1),
            title.events[NoteSaved],
        )
        await projection.apply(
            None,
            likes,
            NoteDeleted(note_id=note, tenant_id=tenant, version=100),
            likes.events[NoteDeleted],
        )
        view: ProjectionView[NoteRow] = ProjectionView(
            projection, NoteRow, None, wait_ms=20, token=token
        )
        assert await view.get({"note_id": note}) == NoteRow(note_id=note, title="old")
        assert view.stale is True


async def test_a_waiting_read_rereads_until_its_group_catches_up(
    memory_store: MemoryDocumentStoreProvider,
) -> None:
    """A cached row behind the token is reread while waiting: v2 arriving
    10 ms into a 100 ms budget is served fresh (stale=False)."""
    projection = _store_projection()
    group = projection.groups[0]
    saved = group.events[NoteSaved]
    tenant, note = uuid4(), uuid4()
    key = {"tenant_id": tenant, "note_id": note}
    header = issue_token(
        [(projection.name, key, "note_version", note, 2)], tenant_id=tenant, keys=KEYS
    )
    token = read_token(header, tenant_id=tenant, keys=KEYS)
    assert isinstance(token, ConsistencyToken)

    async def later_v2() -> None:
        await asyncio.sleep(0.01)
        v2 = NoteSaved(note_id=note, tenant_id=tenant, title="v2", version=2)
        await projection.apply(None, group, v2, saved)

    with bind_tenant(tenant):
        v1 = NoteSaved(note_id=note, tenant_id=tenant, title="v1", version=1)
        await projection.apply(None, group, v1, saved)
        view: ProjectionView[NoteRow] = ProjectionView(
            projection, NoteRow, None, wait_ms=100, token=token, response=Response()
        )
        writer = asyncio.create_task(later_v2())
        page = await view.query()
        await writer
    assert [r.title for r in page.items] == ["v2"]
    assert view.stale is False


@pytest_asyncio.fixture
async def committed_index(db_engine: Any) -> AsyncIterator[Any]:
    """The relational index as a COMMITTED table: another session's commit
    is what a waiting read must see (a session factory on the engine)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with db_engine.begin() as conn:
        await conn.run_sync(
            lambda sync: FixtureBase.metadata.create_all(
                sync, tables=[NoteIndex.__table__]
            )
        )
    try:
        yield async_sessionmaker(db_engine, expire_on_commit=False)
    finally:
        async with db_engine.begin() as conn:
            await conn.run_sync(
                lambda sync: FixtureBase.metadata.drop_all(
                    sync, tables=[NoteIndex.__table__]
                )
            )


async def test_a_waiting_relational_read_reloads_the_rows_it_already_holds(
    committed_index: Any,
) -> None:
    """G-MSTORE-07 on a relational target: the reader's session already
    holds the v1 row (ORM identity map); v2 is committed by another session
    10 ms into a 100 ms wait. The reread must RELOAD the row: the query and
    the get serve v2 fresh (stale=False), never the cached v1."""
    sessions = committed_index
    saved = SAVED_GROUP.events[NoteSaved]
    tenant, note = uuid4(), uuid4()
    key = {"tenant_id": tenant, "note_id": note}

    def token_for(version: int) -> ConsistencyToken:
        header = issue_token(
            [(NOTE_INDEX.name, key, "note_version", note, version)],
            tenant_id=tenant,
            keys=KEYS,
        )
        token = read_token(header, tenant_id=tenant, keys=KEYS)
        assert isinstance(token, ConsistencyToken)
        return token

    async def commit(version: int, delay: float = 0.0) -> None:
        await asyncio.sleep(delay)
        async with sessions() as writer:
            with bind_tenant(tenant):
                event = NoteSaved(
                    note_id=note, tenant_id=tenant, title=f"v{version}", version=version
                )
                await NOTE_INDEX.apply(writer, SAVED_GROUP, event, saved)
            await writer.commit()

    await commit(1)
    async with sessions() as reader:
        with bind_tenant(tenant):
            plain: ProjectionView[NoteRow] = ProjectionView(
                NOTE_INDEX, NoteRow, reader, wait_ms=0, token=None, response=Response()
            )
            assert await plain.get({"note_id": note}) == NoteRow(
                note_id=note, title="v1"
            )

            paged: ProjectionView[NoteRow] = ProjectionView(
                NOTE_INDEX,
                NoteRow,
                reader,
                wait_ms=100,
                token=token_for(2),
                response=Response(),
            )
            writer = asyncio.create_task(commit(2, delay=0.01))
            page = await paged.query(filter={"tenant_id": tenant})
            await writer
            assert [r.title for r in page.items] == ["v2"]
            assert paged.stale is False

            await commit(3)
            got: ProjectionView[NoteRow] = ProjectionView(
                NOTE_INDEX,
                NoteRow,
                reader,
                wait_ms=100,
                token=token_for(3),
                response=Response(),
            )
            assert await got.get({"note_id": note}) == NoteRow(note_id=note, title="v3")
            assert got.stale is False


def test_the_token_digest_is_the_kernels() -> None:
    key = {"tenant_id": uuid4(), "note_id": uuid4()}
    assert ConsistencyToken.digest(key) == key_digest(
        {k: str(v) for k, v in key.items()}
    )


def test_a_write_route_sets_the_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant, note = uuid4(), uuid4()
    payload = {
        "note_id": str(note),
        "tenant_id": str(tenant),
        "title": "t",
        "version": 1,
    }
    descriptor = NOTE_INDEX.descriptor("sdkfix_notes")
    plan = route_runtime._Plan.__new__(route_runtime._Plan)
    plan.tokens = {NoteSaved.channel: ((NOTE_INDEX.name, descriptor.token_entries),)}

    class _Session:  # hashable, like a real session
        info = {
            _OUTBOX_PENDING_KEY: [
                _PendingEvent(None, NoteSaved.channel, "v1", payload)  # type: ignore[arg-type]
            ]
        }

    session = _Session()
    scope = SimpleNamespace(lazy=SimpleNamespace(session=session))
    monkeypatch.setattr(tokens_module, "_keys", lambda keys: KEYS)
    response = Response()
    with bind_tenant(tenant):
        route_runtime._issue_token(plan, scope, response)  # type: ignore[arg-type]
    token = read_token(
        response.headers["SANDLOOM-Consistency-Token"], tenant_id=tenant, keys=KEYS
    )
    assert token is not None and token.entries[0].version == 1
    assert token.entries[0].group == "note_version"
