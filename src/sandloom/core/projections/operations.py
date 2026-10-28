"""Projection operations on a running core (spec §6.4 items 5 and 7): the
rebuild and tombstone-purge entry points, the purge floor computed from the
deployment, the watermark guard on the apply path and the consistency token
of a write.

* :func:`rebuild_projection` runs the registered rebuilder with the
  projection marked "rebuild in progress" in its watermark row (a purge
  refuses meanwhile); the watermark is never lowered.
* :func:`purge_projection_tombstones` computes the purge floor (stream
  retention + relay horizon, the oldest DLQ entry and the oldest dead outbox
  row of the source channels), raises the watermark, then runs the
  projection's ``purge`` (its descriptor's tombstone delete).
* :func:`watermark_guarded` wraps the consumers of a projection that purges
  tombstones: an event emitted before the watermark is dead-lettered.
* :func:`response_token` is the ``SANDLOOM-Consistency-Token`` of a write: the
  projection rows its pending outbox events will produce.
"""

from __future__ import annotations

import dataclasses
import functools
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sandloom.core.config.settings import settings
from sandloom.core.effects import relay_retry_horizon_seconds
from sandloom.core.projections.descriptors import (
    ProjectionConsumerDescriptor,
    ProjectionRebuildResult,
)
from sandloom.core.projections.purge import (
    PurgeFloor,
    PurgeRefused,
    check_watermark,
    purge_tombstones,
)
from sandloom.core.projections.rebuild import ProjectionRebuildError, rebuild
from sandloom.core.projections.tokens import ConsistencyToken, issue_token, read_token
from sandloom.core.runtime import get_active_core
from sandloom.core.sql_alchemy.projection_watermarks import (
    RelationalWatermarkStore,
    locked_watermark,
    oldest_dead_outbox_age_s,
)
from sandloom.core.tenant_context import current_tenant_id_optional
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.scope import _OUTBOX_PENDING_KEY


def _core(core: Any | None) -> Any:
    found = core if core is not None else get_active_core()
    if found is None:
        raise RuntimeError("no active core: run inside the application")
    return found


def _descriptor(core: Any, name: str) -> Any:
    descriptor = core.projection_registry.get_projection(name)
    if descriptor is None:
        raise ProjectionRebuildError(f"unknown projection {name!r}")
    return descriptor


def watermark_store(storage: str) -> RelationalWatermarkStore:
    """The watermark rows of the projections stored in database ``storage``
    (each operation commits on its own)."""
    return RelationalWatermarkStore(lambda: db_session(storage))


async def rebuild_projection(
    name: str,
    *,
    tenant_id: uuid.UUID | None = None,
    entity: str | None = None,
    blue_green: bool = False,
    core: Any | None = None,
) -> ProjectionRebuildResult:
    """Rebuild projection ``name`` (one entity, one tenant or everything)
    with the rebuild marked in its watermark row.

    ``blue_green=True`` asks for a fresh physical target and an alias swap;
    no 1.0 store has an alias swap, so the rebuild runs in place
    (convergent: every write is version-guarded) and the result says so
    (``details["blue_green"] == "in_place"``)."""
    core = _core(core)
    descriptor = _descriptor(core, name)
    result = await rebuild(
        core.projection_registry,
        name,
        tenant_id=tenant_id,
        entity=entity,
        watermarks=watermark_store(descriptor.storage),
    )
    if blue_green:
        result.details["blue_green"] = "in_place"
    return result


def _stream_entry_ms(entry_id: Any) -> int | None:
    try:
        return int(str(entry_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return None


async def oldest_dlq_age_s(
    core: Any, channels: Iterable[str], now: datetime | None = None
) -> float:
    """Age in seconds of the oldest dead-lettered event of ``channels`` (by
    its original stream entry), 0 without a DLQ."""
    transport = getattr(core.event_bus, "transport", None)
    if transport is None or not hasattr(transport, "list_dlq"):
        return 0.0
    now_ms = (now or datetime.now(UTC)).timestamp() * 1000.0
    oldest = 0.0
    for channel in channels:
        try:
            entries = await transport.list_dlq(channel, count=1000)
        except NotImplementedError:
            return 0.0
        for entry in entries:
            ms = _stream_entry_ms(entry.get("original_id") or entry.get("id"))
            if ms is not None:
                oldest = max(oldest, (now_ms - ms) / 1000.0)
    return oldest


async def purge_floor(core: Any, descriptor: Any) -> PurgeFloor:
    """The purge floor of ``descriptor``'s projection in this deployment."""
    channels = tuple(descriptor.source_events)
    dead = 0.0
    for database in sorted({descriptor.storage, "default"}):
        async with db_session(database) as session:
            dead = max(dead, await oldest_dead_outbox_age_s(session, channels))
    return PurgeFloor(
        stream_retention_s=float(settings.event_stream_retention_seconds),
        relay_horizon_s=relay_retry_horizon_seconds(settings),
        oldest_dlq_age_s=await oldest_dlq_age_s(core, channels),
        oldest_dead_outbox_age_s=dead,
    )


async def purge_projection_tombstones(
    name: str, *, older_than: timedelta, core: Any | None = None
) -> int:
    """Purge ``name``'s tombstones older than ``older_than`` (refused below
    the purge floor or during a rebuild, :class:`PurgeRefused`)."""
    core = _core(core)
    descriptor = _descriptor(core, name)
    purge = getattr(descriptor, "purge", None)
    if purge is None:
        raise PurgeRefused(f"projection {name!r} declares no tombstones to purge")

    async def delete(cutoff: datetime) -> int:
        async with db_session(descriptor.storage) as session:
            return int(await purge(session, cutoff))

    return await purge_tombstones(
        name,
        older_than=older_than,
        floor=await purge_floor(core, descriptor),
        watermarks=watermark_store(descriptor.storage),
        delete_tombstones=delete,
    )


def watermark_guarded(
    descriptor: ProjectionConsumerDescriptor,
) -> ProjectionConsumerDescriptor:
    """``descriptor`` with a handler that first refuses an event emitted
    before the projection's purge watermark: dead-lettered, never applied.

    The watermark is read in the consumer's own unit under a share lock held
    until the apply commits (:func:`locked_watermark`), and a purge raises it
    under the row's exclusive lock before deleting: the check and the apply
    are atomic with respect to a purge, for a relational model and a store
    target alike (the store write runs inside the locked unit). A purge thus
    never deletes a tombstone between an apply's check and its write."""
    handler = descriptor.handler
    projection = descriptor.projection

    @functools.wraps(handler)
    async def guarded(session: Any, envelope: Any) -> None:
        check_watermark(await locked_watermark(session, projection), envelope)
        await handler(session, envelope)

    return dataclasses.replace(descriptor, handler=guarded)


def read_consistency_token(header: str | None) -> ConsistencyToken | None:
    """The verified token of ``header`` for the bound tenant, ``None``
    otherwise (never an error)."""
    return read_token(header, tenant_id=current_tenant_id_optional())


def response_token(
    sources: Mapping[str, tuple[tuple[str, Any], ...]], sessions: Iterable[Any]
) -> str | None:
    """The token of the events pending in ``sessions`` (the write's units):
    for each event on a source channel, the ``(projection, key, group,
    aggregate, version)`` rows its projections will reach. ``None`` without a
    source event, a bound tenant or a signing key, or with more than 16
    entries."""
    tenant = current_tenant_id_optional()
    if tenant is None:
        return None
    entries: list[tuple[str, Mapping[str, Any], str, Any, int]] = []
    for session in sessions:
        for event in session.info.get(_OUTBOX_PENDING_KEY, ()):
            for projection, entries_of in sources.get(event.channel, ()):
                for key, group, aggregate, version in entries_of(
                    event.channel, event.payload
                ):
                    entries.append((projection, key, group, aggregate, version))
    if not entries:
        return None
    return issue_token(entries, tenant_id=tenant)


__all__ = [
    "oldest_dlq_age_s",
    "purge_floor",
    "purge_projection_tombstones",
    "read_consistency_token",
    "rebuild_projection",
    "response_token",
    "watermark_guarded",
    "watermark_store",
]
