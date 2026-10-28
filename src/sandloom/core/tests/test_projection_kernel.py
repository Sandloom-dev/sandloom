"""The kernel side of projections to any store (spec §6.4): consistency
tokens, the tombstone purge watermark, the rebuild guard and dispatch, and
the ``conditional_write`` conformance kit on the in-memory document driver."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.projections.descriptors import ProjectionRebuildResult
from sandloom.core.projections.purge import (
    OLDER_THAN_WATERMARK,
    MemoryWatermarkStore,
    PurgeFloor,
    PurgeRefused,
    Watermark,
    check_watermark,
    purge_floor_seconds,
    purge_tombstones,
    rebuilding,
)
from sandloom.core.projections.rebuild import (
    ProjectionRebuildError,
    rebuild,
    rebuild_tenants,
)
from sandloom.core.projections.registry import QueryProjectionRegistry
from sandloom.core.projections.stores import (
    ConditionalWriteStore,
    MemoryDocumentStore,
    conditional_write_conformance,
)
from sandloom.core.projections.tokens import (
    MAX_ENTRIES,
    TOKEN_TTL_SECONDS,
    aggregate_digest,
    entries_satisfied,
    issue_token,
    key_digest,
    pending_entries,
    read_token,
)

KEYS = {"2026-01": "old-secret", "2026-10": "new-secret"}
T = uuid.uuid4()


# --------------------------------------------------------------------------- #
# Consistency tokens
# --------------------------------------------------------------------------- #
def test_a_token_round_trips_under_the_newest_kid_and_any_listed_kid() -> None:
    key = {"tenant_id": T, "task_id": "7"}
    token = issue_token(
        [("board.tasks", key, "task_version", "7", 4)], tenant_id=T, keys=KEYS
    )
    assert token is not None
    read = read_token(token, tenant_id=T, keys=KEYS)
    assert read is not None and read.kid == "2026-10"
    assert read.entries == (
        ("board.tasks", key_digest(key), "task_version", aggregate_digest("7"), 4),
    )
    # rotation: the old kid still verifies what it signed
    old = issue_token(
        [("p", key, "v", "7", 1)], tenant_id=T, keys={"2026-01": "old-secret"}
    )
    assert read_token(old, tenant_id=T, keys=KEYS) is not None


def test_a_bad_token_is_ignored_never_an_error() -> None:
    token = issue_token([("p", {"k": 1}, "v", 1, 1)], tenant_id=T, keys=KEYS)
    assert token is not None
    version, body, sig = token.split(".")
    other = uuid.uuid4()
    for bad in (
        None,
        "",
        "garbage",
        f"{version}.{body}.{sig[:-2]}xx",  # forged signature
        f"v9.{body}.{sig}",  # unknown format
    ):
        assert read_token(bad, tenant_id=T, keys=KEYS) is None, bad
    assert read_token(token, tenant_id=other, keys=KEYS) is None  # other tenant
    assert read_token(token, tenant_id=None, keys=KEYS) is None  # unbound
    assert read_token(token, tenant_id=T, keys={"x": "y"}) is None  # unknown kid
    later = lambda: datetime.now(UTC).timestamp() + TOKEN_TTL_SECONDS + 5  # noqa: E731
    assert read_token(token, tenant_id=T, keys=KEYS, now=later) is None  # expired


def test_a_large_write_gets_no_token_and_no_key_means_no_token() -> None:
    many = [("p", {"k": i}, "v", i, 1) for i in range(MAX_ENTRIES + 1)]
    assert issue_token(many, tenant_id=T, keys=KEYS) is None
    assert issue_token(many[:MAX_ENTRIES], tenant_id=T, keys=KEYS) is not None
    assert issue_token([("p", {"k": 1}, "v", 1, 1)], tenant_id=T, keys={}) is None


def test_a_read_waits_only_for_its_own_projections_missing_versions() -> None:
    token = read_token(
        issue_token(
            [
                ("a", {"k": 1}, "v", 1, 3),
                ("a", {"k": 2}, "v", 2, 1),
                ("b", {"k": 1}, "v", 1, 9),
            ],
            tenant_id=T,
            keys=KEYS,
        ),
        tenant_id=T,
        keys=KEYS,
    )
    stored = {key_digest({"k": 1}): 3, key_digest({"k": 2}): None}

    def version(digest: str, group: str) -> int | None:
        assert group == "v"
        return stored.get(digest)

    assert [(e.key, e.version) for e in pending_entries(token, "a", version)] == [
        (key_digest({"k": 2}), 1)
    ]
    stored[key_digest({"k": 2})] = 1
    assert entries_satisfied(token, "a", version)
    assert entries_satisfied(None, "a", version)


async def test_a_wait_compares_each_source_group_with_its_own_version() -> None:
    """Independent source groups version independently: a row whose like
    group is at 100 has NOT reached title version 5 while its title group
    is at 1 (no max() across groups), and the token names group and
    aggregate per entry."""
    key = {"tenant_id": T, "note_id": "n"}
    token = read_token(
        issue_token([("p", key, "title_version", "n", 5)], tenant_id=T, keys=KEYS),
        tenant_id=T,
        keys=KEYS,
    )
    assert token is not None
    (entry,) = token.entries
    assert (entry.group, entry.aggregate) == ("title_version", aggregate_digest("n"))
    row = {"title_version": 1, "like_version": 100}

    async def stored(digest: str, group: str) -> int | None:
        assert digest == key_digest(key)
        return row.get(group)

    assert await token.wait_for("p", stored, wait_ms=0) is False
    row["title_version"] = 5
    assert await token.wait_for("p", stored, wait_ms=0) is True


# --------------------------------------------------------------------------- #
# Purge watermark
# --------------------------------------------------------------------------- #
FLOOR = PurgeFloor(stream_retention_s=7 * 86400, relay_horizon_s=3600)


@pytest.mark.asyncio
async def test_a_purge_below_the_replay_horizon_is_refused() -> None:
    marks = MemoryWatermarkStore()
    deleted: list[datetime] = []

    async def delete(cutoff: datetime) -> int:
        deleted.append(cutoff)
        return 0

    with pytest.raises(PurgeRefused, match="replay horizon"):
        await purge_tombstones(
            "p",
            older_than=timedelta(days=7),
            floor=FLOOR,
            watermarks=marks,
            delete_tombstones=delete,
        )
    dlq = PurgeFloor(7 * 86400, 3600, oldest_dlq_age_s=30 * 86400)
    with pytest.raises(PurgeRefused):
        await purge_tombstones(
            "p",
            older_than=timedelta(days=10),
            floor=dlq,
            watermarks=marks,
            delete_tombstones=delete,
        )
    assert purge_floor_seconds(PurgeFloor(0, 3600)) == float("inf")
    assert deleted == [] and (await marks.get("p")).purged_before is None


@pytest.mark.asyncio
async def test_the_watermark_is_raised_before_the_delete_and_never_lowered() -> None:
    marks = MemoryWatermarkStore()
    now = datetime(2026, 10, 26, 12, 0)
    seen: list[Any] = []

    async def delete(cutoff: datetime) -> int:
        seen.append((await marks.get("p")).purged_before)
        return 3

    removed = await purge_tombstones(
        "p",
        older_than=timedelta(days=30),
        floor=FLOOR,
        watermarks=marks,
        delete_tombstones=delete,
        now=lambda: now,
    )
    first = now - timedelta(days=30)
    assert removed == 3 and seen == [first]  # the watermark committed first
    await purge_tombstones(
        "p",
        older_than=timedelta(days=60),
        floor=FLOOR,
        watermarks=marks,
        delete_tombstones=delete,
        now=lambda: now,
    )
    assert (await marks.get("p")).purged_before == first  # monotonic


@pytest.mark.asyncio
async def test_a_purge_refuses_during_a_rebuild_and_a_rebuild_keeps_the_mark() -> None:
    marks = MemoryWatermarkStore()
    await marks.raise_purged_before("p", datetime(2026, 1, 1))
    registry = QueryProjectionRegistry()

    class Rebuilder:
        async def rebuild_all(self) -> ProjectionRebuildResult:
            with pytest.raises(PurgeRefused, match="rebuild"):
                await purge_tombstones(
                    "p",
                    older_than=timedelta(days=60),
                    floor=FLOOR,
                    watermarks=marks,
                    delete_tombstones=_never,
                )
            return ProjectionRebuildResult(projection="p", rows=5)

        async def rebuild_tenant(self, tenant_id: uuid.UUID) -> ProjectionRebuildResult:
            return ProjectionRebuildResult(projection="p", rows=2)

        async def rebuild_entity(self, ref: str) -> ProjectionRebuildResult:
            return ProjectionRebuildResult(projection="p", rows=1)

    registry.register_rebuilder("p", Rebuilder())
    assert (await rebuild(registry, "p", watermarks=marks)).rows == 5
    assert (await rebuild(registry, "p", tenant_id=T)).rows == 2
    assert (await rebuild(registry, "p", entity="m:1")).rows == 1
    mark = await marks.get("p")
    assert mark == Watermark(datetime(2026, 1, 1), None)  # kept, flag cleared
    with pytest.raises(ProjectionRebuildError):
        await rebuild(registry, "missing")
    summed = await rebuild_tenants(Rebuilder(), "p", [uuid.uuid4(), uuid.uuid4()])
    assert summed.rows == 4 and summed.details == {"tenants": 2}


async def _never(_cutoff: datetime) -> int:
    raise AssertionError("deleted although refused")


async def concurrent_rebuilds_keep_the_purge_excluded(marks: Any) -> None:
    """Rebuild A starts, rebuild B starts, A finishes: B still runs, so a
    purge is refused (a purged v2 tombstone would let B's stale v1 upsert
    resurrect the row); once B finishes too, the purge runs. Shared by the
    in-memory and the relational watermark store (G-MSTORE-09)."""
    deleted: list[datetime] = []

    async def delete(cutoff: datetime) -> int:
        deleted.append(cutoff)
        return 1

    async def purge() -> int:
        return await purge_tombstones(
            "r",
            older_than=timedelta(days=1),
            floor=PurgeFloor(stream_retention_s=60, relay_horizon_s=0),
            watermarks=marks,
            delete_tombstones=delete,
        )

    first, second = rebuilding("r", marks), rebuilding("r", marks)
    await first.__aenter__()
    await second.__aenter__()
    await first.__aexit__(None, None, None)  # A finished, B still running
    assert (await marks.get("r")).rebuild_started_at is not None
    with pytest.raises(PurgeRefused, match="rebuild"):
        await purge()
    assert deleted == []
    await second.__aexit__(None, None, None)
    assert (await marks.get("r")).rebuild_started_at is None
    assert await purge() == 1
    # a stray end never drives the count below zero: the next rebuild counts
    await marks.end_rebuild("r")
    async with rebuilding("r", marks):
        with pytest.raises(PurgeRefused, match="rebuild"):
            await purge()
    assert (await marks.get("r")).rebuild_started_at is None


async def test_concurrent_rebuilds_keep_the_purge_excluded_in_memory() -> None:
    await concurrent_rebuilds_keep_the_purge_excluded(MemoryWatermarkStore())


async def a_rebuild_never_enters_a_running_purge(
    marks: Any, entry_waits: Callable[[], Awaitable[bool]]
) -> None:
    """Round 3 (G-MSTORE-09): the purge has read "no rebuild" and is about
    to raise the watermark and delete when a rebuild starts. The rebuild's
    entry must wait until the purge's raise and delete are done (else the
    rebuild's stale upserts would run with the tombstones half-purged), and
    a purge after it is refused. ``entry_waits()`` says whether the rebuild's
    begin is observed waiting (it is polled while the purge deletes)."""
    events: list[str] = []
    entered: list[asyncio.Task[None]] = []

    async def begin() -> None:
        await marks.begin_rebuild("w", datetime(2026, 10, 26, 12))
        events.append("rebuild began")

    async def delete(cutoff: datetime) -> int:
        entered.append(asyncio.create_task(begin()))
        for _ in range(500):
            if entered[0].done() or await entry_waits():
                break
            await asyncio.sleep(0.01)
        events.append("purge deleted")
        return 1

    purged = await purge_tombstones(
        "w",
        older_than=timedelta(days=1),
        floor=PurgeFloor(stream_retention_s=60, relay_horizon_s=0),
        watermarks=marks,
        delete_tombstones=delete,
    )
    await asyncio.wait_for(entered[0], 10)
    assert purged == 1
    assert events == ["purge deleted", "rebuild began"]
    with pytest.raises(PurgeRefused, match="rebuild"):
        await purge_tombstones(
            "w",
            older_than=timedelta(days=1),
            floor=PurgeFloor(stream_retention_s=60, relay_horizon_s=0),
            watermarks=marks,
            delete_tombstones=_never,
        )
    await marks.end_rebuild("w")


async def test_a_rebuild_never_enters_a_running_purge_in_memory() -> None:
    async def yielded() -> bool:
        # the in-memory begin runs at the next loop turn unless it waits
        await asyncio.sleep(0)
        return True

    await a_rebuild_never_enters_a_running_purge(MemoryWatermarkStore(), yielded)


def test_an_event_older_than_the_watermark_is_dead_lettered() -> None:
    mark = Watermark(purged_before=datetime(2026, 10, 1))
    old = {"id": "e1", "emitted_at": "2026-09-30T23:59:59+00:00"}
    with pytest.raises(PoisonEventError, match=OLDER_THAN_WATERMARK):
        check_watermark(mark, old)
    with pytest.raises(PoisonEventError):
        check_watermark(mark, {"id": "e2"})  # no emitted_at: fail closed
    check_watermark(mark, {"id": "e3", "emitted_at": "2026-10-01T00:00:01Z"})
    check_watermark(Watermark(), old)  # no purge yet: nothing refused


# --------------------------------------------------------------------------- #
# Conditional-write stores
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_memory_document_store_passes_the_conformance_kit() -> None:
    store = MemoryDocumentStore()
    assert isinstance(store, ConditionalWriteStore)
    await conditional_write_conformance(store, deleted_at=datetime(2026, 10, 26))
    # tombstones are purged by age
    key = {"id": "gone"}
    old, new = datetime(2026, 10, 26), datetime(2026, 10, 28)
    tenant = uuid.uuid4()
    for when, ident in ((old, "gone"), (new, "kept")):
        await store.tombstone_if_newer(
            tenant_id=tenant,
            key={"id": ident},
            version_column="v",
            version=1,
            deleted_at=when,
        )
    assert await store.purge_tombstones(datetime(2026, 10, 27)) == 1
    assert await store.version_of(tenant_id=tenant, key=key, version_column="v") is None


@pytest.mark.asyncio
async def test_the_conformance_kit_catches_a_store_whose_upserts_clear_tombstones() -> (
    None
):
    class Resurrecting(MemoryDocumentStore):
        async def put_if_newer(self, **kwargs: Any) -> bool:
            kwargs["fields"] = {**kwargs["fields"], self.deleted_column: None}
            return await super().put_if_newer(**kwargs)

    with pytest.raises(AssertionError, match="never clears a tombstone"):
        await conditional_write_conformance(
            Resurrecting(), deleted_at=datetime(2026, 10, 26)
        )


@pytest.mark.asyncio
async def test_the_conformance_kit_catches_a_store_without_the_version_guard() -> None:
    class Naive(MemoryDocumentStore):
        @staticmethod
        def _newer(row: Any, version_column: str, version: int) -> bool:
            return True  # last write wins: not convergent

    with pytest.raises(AssertionError, match="older version"):
        await conditional_write_conformance(Naive(), deleted_at=datetime(2026, 10, 26))


@pytest.mark.asyncio
async def test_the_memory_driver_advertises_and_passes_the_kit() -> None:
    from sandloom.core.drivers.memory_document_store import (
        MEMORY_DOCUMENT_STORE_FEATURES,
        create_memory_document_store,
    )
    from sandloom.core.infra.config import InstanceConfig
    from sandloom.core.infra.extra_capabilities import DOCUMENT_STORE_CAPABILITY

    assert {"conditional_write", "tenant_native"} <= MEMORY_DOCUMENT_STORE_FEATURES
    assert MEMORY_DOCUMENT_STORE_FEATURES <= DOCUMENT_STORE_CAPABILITY.features
    provider = create_memory_document_store(
        InstanceConfig(capability="document_store", name="docs", driver="memory")
    )
    store = provider.projection("p.one")
    assert provider.projection("p.one") is store  # one store per projection
    await conditional_write_conformance(store, deleted_at=datetime(2026, 10, 26))
    doc_id = await provider.insert("c", {"a": 1})
    assert (await provider.find_one("c", {"a": 1}) or {})["_id"] == doc_id
    assert await provider.update("c", {"a": 1}, {"b": 2}) == 1
    assert await provider.delete("c", {"b": 2}) == 1
