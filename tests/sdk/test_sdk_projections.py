"""Declarative projections (spec §6.4; ledger C-DECL-04, G-MSTORE-08/09).

Validation (SANDLOOM-B012) and the apply path against the database: full-state
events converge under any interleaving with duplicates, tombstones beat late
older upserts, deltas are increments, views never return tombstones.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict
from sdkfix_notes.events import NoteDeleted, NoteLiked, NoteSaved
from sdkfix_notes.models import FixtureBase, NoteIndex
from sdkfix_notes.projections import NOTE_INDEX

from sandloom.sdk import Event
from sandloom.sdk.errors import ProjectionDefinitionError
from sandloom.sdk.projections import (
    Projection,
    ProjectionView,
    SourceGroup,
    delta,
    full_state,
    tombstone,
)
from sandloom.sdk.tenant import bind_tenant

SAVED_GROUP = NOTE_INDEX.groups[0]
LIKES_GROUP = NOTE_INDEX.groups[1]


def _group(**overrides: Any) -> SourceGroup:
    fields: dict[str, Any] = {
        "events": {NoteSaved: full_state(lambda e: {"title": e.title})},
        "aggregate_key": "note_id",
        "version_field": "version",
        "version_column": "note_version",
        "owns": ("title",),
    }
    fields.update(overrides)
    return SourceGroup(**fields)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"key": ("note_id",)}, "must start with 'tenant_id'"),
        ({"model": None}, "needs a target"),
        ({"groups": []}, "at least one"),
        (
            {"groups": [_group(), _group(version_column="like_version")]},
            "owned by groups",
        ),
        ({"groups": [_group(), _group(owns=("likes",))]}, "used by two groups"),
        ({"insert_only": {"title": "x"}}, "both owned and insert_only"),
        ({"groups": [_group(owns=("nope",))]}, "no column"),
        ({"groups": [_group(version_field="missing")]}, "no field 'missing'"),
        (
            {
                "groups": [_group(events={NoteLiked: delta(lambda e: {"likes": 1})})],
                "at_least_once": True,
            },
            "durable inbox",
        ),
        (
            {"groups": [_group(events={NoteSaved: lambda e: {}})]},
            "not full_state, delta or tombstone",
        ),
        (
            {
                "groups": [_group(events={NoteDeleted: tombstone(lambda e: {})})],
                "deleted_column": "gone",
            },
            "tombstones need the column",
        ),
    ],
)
def test_invalid_projections_are_refused(kwargs: dict[str, Any], message: str) -> None:
    args: dict[str, Any] = {
        "key": ("tenant_id", "note_id"),
        "model": NoteIndex,
        "groups": [_group()],
    }
    args.update(kwargs)
    with pytest.raises(ProjectionDefinitionError, match=message):
        Projection("sdkfix.bad", **args)


def test_the_l2_form_is_one_consumer_per_channel() -> None:
    consumers = NOTE_INDEX.consumers()
    assert [c.source_channel for c in consumers] == [
        NoteSaved.channel,
        NoteDeleted.channel,
        NoteLiked.channel,
    ]
    assert all(c.durable and c.bind_tenant_from == "tenant_id" for c in consumers)
    descriptor = NOTE_INDEX.descriptor("sdkfix_notes")
    assert descriptor.source_events == NOTE_INDEX.channels()
    assert descriptor.rebuildable is False


@pytest_asyncio.fixture
async def index_db(db: Any) -> AsyncIterator[Any]:
    def create(sync: Any) -> None:
        FixtureBase.metadata.create_all(sync.connection(), tables=[NoteIndex.__table__])

    await db.run_sync(create)
    yield db


async def _row(db: Any, tenant: UUID, note: UUID) -> Any:
    db.expire_all()
    return await db.get(NoteIndex, (tenant, note))


async def test_full_state_converges_under_shuffles_and_duplicates(
    index_db: Any,
) -> None:
    rng = random.Random(20261025)
    for _ in range(5):
        tenant, note = uuid4(), uuid4()
        events: list[Any] = [
            NoteSaved(note_id=note, tenant_id=tenant, title=f"v{v}", version=v)
            for v in range(1, 6)
        ]
        events += [rng.choice(events) for _ in range(4)]  # redeliveries
        rng.shuffle(events)
        with bind_tenant(tenant):
            for event in events:
                await NOTE_INDEX.apply(
                    index_db, SAVED_GROUP, event, SAVED_GROUP.events[NoteSaved]
                )
            row = await _row(index_db, tenant, note)
        assert (row.title, row.note_version) == ("v5", 5)


async def test_a_tombstone_beats_a_late_older_upsert(index_db: Any) -> None:
    tenant, note = uuid4(), uuid4()
    saved = SAVED_GROUP.events[NoteSaved]
    deleted = SAVED_GROUP.events[NoteDeleted]
    with bind_tenant(tenant):
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteDeleted(note_id=note, tenant_id=tenant, version=3),
            deleted,
        )
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteSaved(note_id=note, tenant_id=tenant, title="old", version=2),
            saved,
        )
        row = await _row(index_db, tenant, note)
        assert row.deleted_at is not None and row.note_version == 3
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteSaved(note_id=note, tenant_id=tenant, title="new", version=4),
            saved,
        )
        row = await _row(index_db, tenant, note)
        assert (row.deleted_at, row.title) == (None, "new")


async def test_deltas_increment_and_versions_never_go_back(index_db: Any) -> None:
    tenant, note = uuid4(), uuid4()
    liked = LIKES_GROUP.events[NoteLiked]
    with bind_tenant(tenant):
        for version in (2, 1, 3):
            await NOTE_INDEX.apply(
                index_db,
                LIKES_GROUP,
                NoteLiked(note_id=note, tenant_id=tenant, version=version),
                liked,
            )
        row = await _row(index_db, tenant, note)
    assert (row.likes, row.like_version) == (3, 3)


class NoteRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    note_id: UUID
    title: str | None


async def test_views_skip_tombstones_and_page(index_db: Any) -> None:
    tenant = uuid4()
    saved = SAVED_GROUP.events[NoteSaved]
    notes = sorted(uuid4() for _ in range(3))
    with bind_tenant(tenant):
        for i, note in enumerate(notes):
            await NOTE_INDEX.apply(
                index_db,
                SAVED_GROUP,
                NoteSaved(note_id=note, tenant_id=tenant, title=f"t{i}", version=1),
                saved,
            )
        await NOTE_INDEX.apply(
            index_db,
            SAVED_GROUP,
            NoteDeleted(note_id=notes[1], tenant_id=tenant, version=2),
            SAVED_GROUP.events[NoteDeleted],
        )
        view: ProjectionView[NoteRow] = ProjectionView(NOTE_INDEX, NoteRow, index_db)
        assert await view.get({"tenant_id": tenant, "note_id": notes[1]}) is None
        found = await view.get({"tenant_id": tenant, "note_id": notes[0]})
        assert found == NoteRow(note_id=notes[0], title="t0")
        first = await view.query(filter={"tenant_id": tenant}, limit=1)
        assert [r.note_id for r in first.items] == [notes[0]]
        assert first.next_cursor is not None
        second = await view.query(
            filter={"tenant_id": tenant}, limit=1, cursor=first.next_cursor
        )
        assert [r.note_id for r in second.items] == [notes[2]]
        assert second.next_cursor is None
        assert view.stale is False


async def test_the_registered_handler_decodes_the_envelope(index_db: Any) -> None:
    tenant, note = uuid4(), uuid4()
    consumer = NOTE_INDEX.consumers()[0]
    envelope = {
        "id": "e1",
        "channel": NoteSaved.channel,
        "payload": {
            "note_id": str(note),
            "tenant_id": str(tenant),
            "title": "via handler",
            "version": 1,
            "unknown_later_field": True,
        },
    }
    with bind_tenant(tenant):
        await consumer.handler(index_db, envelope)
        row = await _row(index_db, tenant, note)
    assert row.title == "via handler"


class _MemberSaved(Event, channel="sdkfix.member.saved.v1"):
    organization_id: UUID
    user_id: UUID
    version: int


class _MemberSavedLoose(Event, channel="sdkfix.member.loose.v1"):
    organization_id: UUID
    user_id: UUID
    version: int | None = None


def _member_group(event: type[Event]) -> SourceGroup:
    return SourceGroup(
        events={event: full_state(lambda e: {"note_id": e.user_id, "title": "m"})},
        aggregate_key="user_id",
        version_field="version",
        version_column="note_version",
        owns=("title",),
        tenant_from="organization_id",
    )


def test_a_source_names_its_tenant_field() -> None:
    """A platform event that carries ``organization_id`` (not ``tenant_id``)
    feeds a projection: the consumer binds that tenant and the row key's
    tenant_id is read from it."""
    projection = Projection(
        "sdkfix.members",
        key=("tenant_id", "note_id"),
        model=NoteIndex,
        groups=[_member_group(_MemberSaved)],
    )
    (consumer,) = projection.consumers()
    assert consumer.bind_tenant_from == "organization_id"
    org, user = uuid4(), uuid4()
    event = _MemberSaved(organization_id=org, user_id=user, version=3)
    group = projection.groups[0]
    key, values, version = projection._row(group, event, group.events[_MemberSaved])
    assert key == {"tenant_id": org, "note_id": user}
    assert values == {"title": "m"} and version == 3


def test_an_optional_version_is_refused_with_the_alternative() -> None:
    with pytest.raises(ProjectionDefinitionError, match="member_event_action"):
        Projection(
            "sdkfix.members_loose",
            key=("tenant_id", "note_id"),
            model=NoteIndex,
            groups=[_member_group(_MemberSavedLoose)],
        )
    with pytest.raises(ProjectionDefinitionError, match="tenant_from"):
        Projection(
            "sdkfix.members_untenanted",
            key=("tenant_id", "note_id"),
            model=NoteIndex,
            groups=[
                SourceGroup(
                    events={_MemberSaved: full_state(lambda e: {"title": "m"})},
                    aggregate_key="user_id",
                    version_field="version",
                    version_column="note_version",
                    owns=("title",),
                )
            ],
        )
