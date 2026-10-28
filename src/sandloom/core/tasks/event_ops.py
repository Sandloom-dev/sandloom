"""Outbox / Streams repair operations, shared by the CLI and the owner console.

``sandloom outbox`` / ``sandloom streams`` (:mod:`sandloom.core.cli.events`) and the
platform owner console's operations API
(:mod:`sandloom.core.admin.api.platform`) run the same operations against a built
core: list / requeue dead-lettered outbox rows, purge delivered ones, list /
replay a Streams dead-letter queue, inspect consumer groups and the
subscription inventory, release a group's owner fingerprint. They reach the
outbox store and the transport through the core's public surface; living in
the messaging layer keeps the console from importing the CLI (ADR-0007).
"""

from __future__ import annotations

import uuid
from typing import Any


class EventOperationUnavailable(SystemExit):
    """The deployment cannot perform this outbox/Streams operation.

    A ``SystemExit`` so the CLIs keep exiting with the message; the owner
    console's operations API catches it and answers ``501``.
    """


def outbox_target(core: Any, database: str) -> tuple[Any, Any]:
    store = core.database(database).outbox()
    if store is None:
        raise EventOperationUnavailable(
            f"error: database {database!r} has no transactional outbox"
        )
    return core.get_uow_factory(database), store


async def outbox_list_dead(
    core: Any,
    *,
    database: str = "default",
    channel: str | None = None,
    limit: int = 100,
) -> list[Any]:
    factory, store = outbox_target(core, database)
    return list(await store.list_dead(factory, channel=channel, limit=limit))


async def outbox_requeue(
    core: Any,
    *,
    database: str = "default",
    ids: list[uuid.UUID] | None = None,
    channel: str | None = None,
    limit: int = 1000,
) -> int:
    factory, store = outbox_target(core, database)
    return int(await store.requeue_dead(factory, ids=ids, channel=channel, limit=limit))


def event_transport(core: Any) -> Any:
    transport = getattr(core.event_bus, "transport", None)
    if transport is None or not hasattr(transport, "replay_dlq"):
        raise EventOperationUnavailable(
            "error: the configured event bus has no durable transport/DLQ "
            "(EVENT_BUS_BACKEND=streams required)"
        )
    return transport


async def streams_dlq_list(
    core: Any, channel: str, *, limit: int = 100, after: str | None = None
) -> list[Any]:
    return list(
        await event_transport(core).list_dlq(
            channel, count=limit, **({"after": after} if after else {})
        )
    )


async def streams_dlq_replay(
    core: Any,
    channel: str,
    *,
    ids: list[str] | None = None,
    limit: int = 100,
    group: str | None = None,
) -> int:
    transport = event_transport(core)
    return int(await transport.replay_dlq(channel, ids=ids, count=limit, group=group))


async def streams_groups(core: Any, channel: str) -> list[dict[str, Any]]:
    return list(await event_transport(core).group_info(channel))


async def streams_consumers(
    core: Any, *, include_stale: bool = False
) -> list[dict[str, Any]]:
    return list(await event_transport(core).list_consumers(include_stale=include_stale))


async def streams_release_group(core: Any, channel: str, group: str) -> bool:
    transport = event_transport(core)
    release = getattr(transport, "release_group_owner", None)
    if release is None:
        raise SystemExit("error: the transport does not track group ownership")
    return bool(await release(channel, group))


async def outbox_purge(
    core: Any,
    *,
    database: str = "default",
    hours: float | None = None,
    batch: int = 1000,
) -> int:
    """Delete delivered outbox rows older than ``hours`` (retention default)."""
    from sandloom.core.tasks.relay import (  # noqa: PLC0415 - cold: the relay registers its metrics on import; an app that never purges must not expose them
        purge_delivered,
        purge_expired_commands,
        purge_expired_requests,
    )

    factory, store = outbox_target(core, database)
    # On demand: drain everything due (no time budget).
    total = await purge_delivered(
        factory,
        store,
        retention_hours=hours,
        batch_size=batch,
        time_budget_seconds=None,
    )
    # Expired command-idempotency records (CONTRACT_COMMAND_DEDUPE_TTL_SECONDS)
    # share the housekeeping run; they are reported in the log, not the count.
    await purge_expired_commands(
        core, database, factory, batch_size=batch, time_budget_seconds=None
    )
    await purge_expired_requests(
        core, database, factory, batch_size=batch, time_budget_seconds=None
    )
    return total


__all__ = [
    "EventOperationUnavailable",
    "event_transport",
    "outbox_list_dead",
    "outbox_purge",
    "outbox_requeue",
    "outbox_target",
    "streams_consumers",
    "streams_dlq_list",
    "streams_dlq_replay",
    "streams_groups",
    "streams_release_group",
]
