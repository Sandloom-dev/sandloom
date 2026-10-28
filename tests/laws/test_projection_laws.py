"""Projection convergence laws (spec §8.2; the model is §6.4).

Through the real L1 ``sandloom.sdk.projections.Projection`` (validation, SANDLOOM-B012)
and its apply path (kernel ``monotonic_upsert`` / ``monotonic_delete`` and the
delta upsert) on real PostgreSQL. Properties:

* **full-state convergence** (§6.4 item 3): for any interleaving of a
  projection's full-state events with duplicates, the final row equals the
  max-version event's fields per source group;
* **field ownership** (§6.4 item 2): a column owned by two groups, or both
  owned and ``insert_only``, is refused with SANDLOOM-B012; at apply time a mapper
  writing a column its group does not own is refused;
* **deltas only with the inbox** (§6.4 item 4): a delta mapper is accepted
  iff the projection's consumers are durable (refused, SANDLOOM-B012, with
  ``at_least_once=True``); applied under the durable inbox claim, any
  duplicated / shuffled delivery counts each delta exactly once;
* **tombstones beat late older upserts** (§6.4 item 5): whatever the order
  and duplication, a tombstone that is the newest event of its group hides the
  row, and no older upsert (late or redelivered) resurrects it.

"A delta mapper on any other target is SANDLOOM-B012" (``Projection(store=...)``)
is pinned in ``tests/sdk/test_sdk_projection_ops.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, Integer, String, Table, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.sdk import Event, TenantScoped
from sandloom.sdk.effects import durable_inbox
from sandloom.sdk.errors import ProjectionDefinitionError
from sandloom.sdk.projections import (
    Mapper,
    Projection,
    ProjectionView,
    SourceGroup,
    delta,
    full_state,
    tombstone,
)
from sandloom.sdk.tenant import bind_tenant
from sandloom.sdk.testing.fixtures import *  # noqa: F403

DB_LAWS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
PURE_LAWS = settings(max_examples=200, deadline=None)

# --------------------------------------------------------------------------- #
# The read model and its events (private base: never in framework metadata)
# --------------------------------------------------------------------------- #


class _LawsBase(DeclarativeBase):
    pass


class LawsItemIndex(_LawsBase, TenantScoped):
    __tablename__ = "laws_projection_item_index"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(50), nullable=True)
    body: Mapped[str | None] = mapped_column(String(50), nullable=True)
    item_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tag: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tag_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    like_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ItemSaved(Event, channel="lawsproj.item.saved.v1"):
    item_id: UUID
    tenant_id: UUID
    title: str
    body: str
    version: int


class ItemDeleted(Event, channel="lawsproj.item.deleted.v1"):
    item_id: UUID
    tenant_id: UUID
    version: int


class ItemScored(Event, channel="lawsproj.item.scored.v1"):
    item_id: UUID
    tenant_id: UUID
    score: int
    version: int


class ItemLiked(Event, channel="lawsproj.item.liked.v1"):
    item_id: UUID
    tenant_id: UUID
    version: int


class ItemTouched(Event, channel="lawsproj.item.touched.v1"):
    """A generic event for the definition-only properties."""

    item_id: UUID
    tenant_id: UUID
    version: int


ITEM_GROUP = SourceGroup(
    events={
        ItemSaved: full_state(lambda e: {"title": e.title, "body": e.body}),
        ItemDeleted: tombstone(lambda e: {}),
    },
    aggregate_key="item_id",
    version_field="version",
    version_column="item_version",
    owns=("title", "body"),
)
SCORE_GROUP = SourceGroup(
    events={ItemScored: full_state(lambda e: {"score": e.score})},
    aggregate_key="item_id",
    version_field="version",
    version_column="score_version",
    owns=("score",),
)
LIKES_GROUP = SourceGroup(
    events={ItemLiked: delta(lambda e: {"likes": 1})},
    aggregate_key="item_id",
    version_field="version",
    version_column="like_version",
    owns=("likes",),
)
ITEM_INDEX = Projection(
    "lawsproj.items",
    key=("tenant_id", "item_id"),
    model=LawsItemIndex,
    groups=[ITEM_GROUP, SCORE_GROUP, LIKES_GROUP],
)
GROUP_OF: dict[type[Event], SourceGroup] = {
    ItemSaved: ITEM_GROUP,
    ItemDeleted: ITEM_GROUP,
    ItemScored: SCORE_GROUP,
    ItemLiked: LIKES_GROUP,
}


class ItemRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: UUID
    title: str | None
    body: str | None
    score: int | None


@pytest_asyncio.fixture
async def index_db(db: Any) -> AsyncIterator[Any]:
    """``db`` with the read model's table (created in the rolled-back test)."""

    def create(sync: Any) -> None:
        _LawsBase.metadata.create_all(
            sync.connection(), tables=[cast(Table, LawsItemIndex.__table__)]
        )

    await db.run_sync(create)
    yield db


async def _apply(db: Any, event: Event) -> None:
    group = GROUP_OF[type(event)]
    await ITEM_INDEX.apply(db, group, event, group.events[type(event)])


async def _row(db: Any, tenant: UUID, item: UUID) -> LawsItemIndex | None:
    db.expire_all()
    found: LawsItemIndex | None = await db.get(LawsItemIndex, (tenant, item))
    return found


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

TEXT = st.text(alphabet="abcdefghij", min_size=0, max_size=8)
VERSIONS = st.sets(st.integers(min_value=1, max_value=60), min_size=1, max_size=5)


@st.composite
def aggregate_histories(draw: st.DrawFn) -> dict[str, Any]:
    """One aggregate's events: per group, one event per (unique) version."""
    item_versions = sorted(draw(VERSIONS))
    score_versions = sorted(draw(VERSIONS))
    return {
        "items": [(v, draw(TEXT), draw(TEXT)) for v in item_versions],
        "scores": [(v, draw(st.integers(-1000, 1000))) for v in score_versions],
    }


def _with_redeliveries(
    data: st.DataObject, events: list[Event], *, max_dups: int = 6
) -> list[Event]:
    """``events`` plus redeliveries of some of them, in an arbitrary order."""
    dups = data.draw(
        st.lists(st.sampled_from(events), max_size=max_dups), label="redeliveries"
    )
    return data.draw(st.permutations([*events, *dups]), label="delivery order")


# --------------------------------------------------------------------------- #
# Full-state convergence (§6.4 item 3)
# --------------------------------------------------------------------------- #


@DB_LAWS
@given(
    histories=st.lists(aggregate_histories(), min_size=1, max_size=3),
    data=st.data(),
)
async def test_full_state_converges_to_the_max_version_event_per_group(
    index_db: Any, histories: list[dict[str, Any]], data: st.DataObject
) -> None:
    tenant = uuid4()
    items = [uuid4() for _ in histories]
    events: list[Event] = []
    for item, history in zip(items, histories, strict=True):
        events += [
            ItemSaved(item_id=item, tenant_id=tenant, title=t, body=b, version=v)
            for v, t, b in history["items"]
        ]
        events += [
            ItemScored(item_id=item, tenant_id=tenant, score=s, version=v)
            for v, s in history["scores"]
        ]
    deliveries = _with_redeliveries(data, events, max_dups=8)

    with bind_tenant(tenant):
        for event in deliveries:
            await _apply(index_db, event)
        for item, history in zip(items, histories, strict=True):
            row = await _row(index_db, tenant, item)
            assert row is not None
            v_item, title, body = max(history["items"])
            v_score, score = max(history["scores"])
            assert (row.title, row.body, row.item_version) == (title, body, v_item)
            assert (row.score, row.score_version) == (score, v_score)
            assert row.deleted_at is None


# --------------------------------------------------------------------------- #
# Tombstones beat late older upserts (§6.4 item 5)
# --------------------------------------------------------------------------- #


@st.composite
def item_histories_with_tombstones(
    draw: st.DrawFn,
) -> list[tuple[int, str, str | None]]:
    """``(version, kind, title)`` with unique versions and >= 1 tombstone."""
    versions = sorted(
        draw(st.sets(st.integers(min_value=1, max_value=60), min_size=1, max_size=6))
    )
    kinds = draw(
        st.lists(
            st.sampled_from(["upsert", "tombstone"]),
            min_size=len(versions),
            max_size=len(versions),
        )
    )
    kinds[draw(st.integers(min_value=0, max_value=len(versions) - 1))] = "tombstone"
    return [
        (v, k, draw(TEXT) if k == "upsert" else None)
        for v, k in zip(versions, kinds, strict=True)
    ]


@DB_LAWS
@given(history=item_histories_with_tombstones(), data=st.data())
async def test_a_tombstone_beats_any_late_older_upsert(
    index_db: Any, history: list[tuple[int, str, str | None]], data: st.DataObject
) -> None:
    tenant, item = uuid4(), uuid4()

    def event(version: int, kind: str, title: str | None) -> Event:
        if kind == "tombstone":
            return ItemDeleted(item_id=item, tenant_id=tenant, version=version)
        assert title is not None
        return ItemSaved(
            item_id=item, tenant_id=tenant, title=title, body="", version=version
        )

    events = [event(*h) for h in history]
    deliveries = _with_redeliveries(data, events)
    newest_version, newest_kind, newest_title = max(history)
    view: ProjectionView[ItemRow] = ProjectionView(ITEM_INDEX, ItemRow, index_db)
    key = {"tenant_id": tenant, "item_id": item}

    async def check() -> None:
        row = await _row(index_db, tenant, item)
        assert row is not None and row.item_version == newest_version
        if newest_kind == "tombstone":
            assert row.deleted_at is not None
            assert await view.get(key) is None
        else:
            assert row.deleted_at is None and row.title == newest_title
            found = await view.get(key)
            assert found is not None and found.title == newest_title

    with bind_tenant(tenant):
        for e in deliveries:
            await _apply(index_db, e)
        await check()
        # every older upsert arrives (again) late: nothing changes
        for version, kind, title in history:
            if kind == "upsert" and version < newest_version:
                await _apply(index_db, event(version, kind, title))
        await check()


# --------------------------------------------------------------------------- #
# Field ownership (§6.4 item 2)
# --------------------------------------------------------------------------- #

OWNABLE = ("title", "body", "score", "tag")
VERSION_COLUMNS = ("item_version", "score_version", "tag_version")


def _touch_group(
    index: int, owns: tuple[str, ...], kind: str = "full_state"
) -> SourceGroup:
    wrap = {"full_state": full_state, "delta": delta, "tombstone": tombstone}[kind]

    def fields(event: ItemTouched) -> dict[str, Any]:
        return dict.fromkeys(owns)

    return SourceGroup(
        events={ItemTouched: wrap(fields)},
        aggregate_key="item_id",
        version_field="version",
        version_column=VERSION_COLUMNS[index],
        owns=owns,
    )


@PURE_LAWS
@given(
    owned=st.lists(
        st.sets(st.sampled_from(OWNABLE), max_size=3), min_size=1, max_size=3
    ),
    insert_only=st.sets(st.sampled_from(OWNABLE), max_size=2),
)
def test_a_column_has_exactly_one_owner_or_the_projection_is_refused(
    owned: list[set[str]], insert_only: set[str]
) -> None:
    groups = [_touch_group(i, tuple(sorted(cols))) for i, cols in enumerate(owned)]
    all_owned = [c for cols in owned for c in cols]
    disjoint = len(all_owned) == len(set(all_owned))
    valid = disjoint and not (set(all_owned) & insert_only)

    def build() -> Projection:
        return Projection(
            "lawsproj.ownership",
            key=("tenant_id", "item_id"),
            model=LawsItemIndex,
            groups=groups,
            insert_only=dict.fromkeys(insert_only, "seed"),
        )

    if valid:
        built = build()
        owners: dict[str, int] = {}
        for index, group in enumerate(built.groups):
            for column in group.owns:
                assert column not in owners
                owners[column] = index
        return
    with pytest.raises(ProjectionDefinitionError) as refused:
        build()
    assert refused.value.sandloom_code == "SANDLOOM-B012"
    assert "owned by groups" in str(refused.value) or "insert_only" in str(
        refused.value
    )


@DB_LAWS
@given(writes=st.sets(st.sampled_from(("title", "body", "score")), min_size=1))
async def test_a_mapper_writing_a_column_its_group_does_not_own_is_refused(
    index_db: Any, writes: set[str]
) -> None:
    """``ITEM_GROUP`` owns ``title`` and ``body``; its mapper writes ``writes``."""
    tenant, item = uuid4(), uuid4()
    mapper: Mapper[ItemSaved] = full_state(
        lambda e: {c: ("x" if c != "score" else 1) for c in writes}
    )
    event = ItemSaved(item_id=item, tenant_id=tenant, title="t", body="b", version=1)
    with bind_tenant(tenant):
        if writes <= set(ITEM_GROUP.owns):
            assert await ITEM_INDEX.apply(index_db, ITEM_GROUP, event, mapper)
            row = await _row(index_db, tenant, item)
            assert row is not None
            assert {c: getattr(row, c) for c in writes} == dict.fromkeys(writes, "x")
            return
        with pytest.raises(ProjectionDefinitionError, match="does not own"):
            await ITEM_INDEX.apply(index_db, ITEM_GROUP, event, mapper)
        assert await _row(index_db, tenant, item) is None


# --------------------------------------------------------------------------- #
# Deltas only with the durable inbox (§6.4 item 4)
# --------------------------------------------------------------------------- #


@PURE_LAWS
@given(
    kinds=st.lists(
        st.sampled_from(["full_state", "delta", "tombstone"]), min_size=1, max_size=3
    ),
    at_least_once=st.booleans(),
)
def test_a_delta_mapper_is_accepted_only_under_the_durable_inbox(
    kinds: list[str], at_least_once: bool
) -> None:
    groups = [_touch_group(i, (OWNABLE[i],), kind) for i, kind in enumerate(kinds)]

    def build() -> Projection:
        return Projection(
            "lawsproj.deltas",
            key=("tenant_id", "item_id"),
            model=LawsItemIndex,
            groups=groups,
            at_least_once=at_least_once,
        )

    if "delta" in kinds and at_least_once:
        with pytest.raises(ProjectionDefinitionError, match="durable inbox") as refused:
            build()
        assert refused.value.sandloom_code == "SANDLOOM-B012"
        return
    consumers = build().consumers()
    assert len(consumers) == len(kinds)
    if "delta" in kinds:
        assert all(c.durable for c in consumers)


@DB_LAWS
@given(
    versions=st.sets(st.integers(min_value=1, max_value=60), min_size=1, max_size=6),
    data=st.data(),
)
async def test_deltas_under_the_inbox_count_each_event_exactly_once(
    index_db: Any, versions: set[int], data: st.DataObject
) -> None:
    """Each delta envelope is delivered 1..3 times in any order; the consumer
    claims the durable inbox in its own unit before applying (as a durable
    consumer does), so the count is the number of distinct events."""
    tenant, item = uuid4(), uuid4()
    consumer = next(
        c for c in ITEM_INDEX.consumers() if c.source_channel == ItemLiked.channel
    )
    assert consumer.durable
    inbox = durable_inbox(consumer.name)
    envelopes = [
        {
            "id": f"laws-like-{uuid4()}",
            "channel": ItemLiked.channel,
            "payload": {"item_id": str(item), "tenant_id": str(tenant), "version": v},
        }
        for v in sorted(versions)
    ]
    copies = data.draw(
        st.lists(
            st.integers(min_value=1, max_value=3),
            min_size=len(envelopes),
            max_size=len(envelopes),
        ),
        label="copies",
    )
    deliveries = data.draw(
        st.permutations(
            [e for e, n in zip(envelopes, copies, strict=True) for _ in range(n)]
        ),
        label="delivery order",
    )
    with bind_tenant(tenant):
        for envelope in deliveries:
            if await inbox.claim(index_db, str(envelope["id"])):
                await consumer.handler(index_db, envelope)
        row = await _row(index_db, tenant, item)
    assert row is not None
    assert (row.likes, row.like_version) == (len(versions), max(versions))
