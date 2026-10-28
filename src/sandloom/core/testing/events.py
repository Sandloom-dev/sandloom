"""Event seams for tests: drive an event through the consumers; ad-hoc schemas.

:func:`deliver_event` pushes one envelope through every consumer registered
for a channel (``register_event_consumer`` and projection consumers) exactly
as the consumer runtime delivers it: schema preflight, the handler's own Unit
of Work, the ``durable=True`` exactly-once claim, the tenant binding — so a
test proves the end-to-end path without starting a bus::

    applied = await deliver_event("billing.subscription.created.v1", payload)
    await deliver_event(channel, payload, times=2)        # a redelivery
    await deliver_event(channel, payload, session=db)     # in the test's tx

Every versioned channel needs a registered schema in every profile (dev =
prod, SANDLOOM-R003): a module registers its own through ``events.EVENT_SCHEMAS``.
A test that publishes on a channel no module owns (a kernel test, a probe)
registers one explicitly::

    register_event_schemas(["probe.thing.happened.v1"])          # any object
    register_event_schemas({"probe.thing.v1": {"type": "object",
                                               "required": ["id"]}})
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import sandloom.core.runtime as _runtime
from sandloom.core.consumers.runtime import ConsumerRuntime
from sandloom.core.testing._uow import bind_test_uow
from sandloom.core.unit_of_work.scope import (
    _AUDIT_STORE_KEY,
    _OUTBOX_STORE_KEY,
)

#: The permissive schema used for a channel registered without one.
ANY_OBJECT: dict[str, Any] = {"type": "object"}


def register_event_schemas(
    channels: Mapping[str, dict[str, Any] | None] | Iterable[str],
    *,
    target: Any = None,
) -> None:
    """Register schemas for ``channels`` (a list = permissive object schemas).

    ``target`` is where they go: a ``CoreFacade`` (``register_event_schema``),
    an event bus (its schema registry) or a schema registry; default: the
    active application's event bus. Re-registering an identical schema is a
    no-op.
    """
    schemas: dict[str, dict[str, Any]] = (
        {c: (s or ANY_OBJECT) for c, s in channels.items()}
        if isinstance(channels, Mapping)
        else dict.fromkeys(channels, ANY_OBJECT)
    )
    if target is None:
        target = _runtime.active_event_bus()
    for channel, schema in schemas.items():
        if hasattr(target, "register_event_schema"):
            target.register_event_schema(channel=channel, name=channel, schema=schema)
        else:
            registry = getattr(target, "_schema_registry", target)
            registry.register(channel, channel, schema)


def _opener_for(session: Any) -> Any:
    """A session opener that yields ``session`` bound as the Unit of Work
    (keeping the stores a ``recording_uow()`` / ``db_uow`` already gave it)."""

    @asynccontextmanager
    async def _open(_database: str) -> AsyncIterator[Any]:
        with bind_test_uow(
            session,
            outbox=session.info.get(_OUTBOX_STORE_KEY),
            audit=session.info.get(_AUDIT_STORE_KEY),
        ):
            yield session

    return _open


async def deliver_event(
    channel: str,
    payload: dict[str, Any],
    *,
    times: int = 1,
    event_id: str | None = None,
    session: Any = None,
    core: Any = None,
) -> int:
    """Deliver one event to every consumer of ``channel``; returns how many.

    The envelope id is stable across ``times`` (a redelivery): a
    ``durable=True`` consumer applies it once, an idempotent one sees it
    ``times`` times. ``session`` runs each delivery in the given (test)
    session instead of a fresh committing Unit of Work. The payload is
    validated against the channel's schema first, like a real publish
    (SANDLOOM-R003 / SANDLOOM-R004). Raises ``LookupError`` when no consumer is
    registered for the channel. A handler's exception propagates.
    """
    core = core if core is not None else _runtime.get_active_core()
    if core is None:
        raise RuntimeError(
            "deliver() needs the test application (no active core): "
            "request the `client` (or `sandloom_app`) fixture in this test, or "
            "pass core="
        )
    bus = getattr(core, "event_bus", None) or _runtime.active_event_bus()
    bus.preflight(channel, payload)
    runtime = ConsumerRuntime(
        core,
        session_opener=_opener_for(session) if session is not None else None,
        startup_timeout=0.0,
        retry_delay=0.0,
    )
    records = [
        record
        for record in [*runtime._event_records(), *runtime._projection_records()]
        if record.channel == channel and record.callback is not None
    ]
    if not records:
        raise LookupError(
            f"no consumer is registered for {channel!r} "
            "(register_event_consumer / register_projection_consumer)"
        )
    envelope = {
        "id": event_id or str(uuid.uuid4()),
        "channel": channel,
        "schema_version": channel.rsplit(".", 1)[-1],
        "payload": payload,
        "emitted_at": datetime.now(UTC).isoformat(),
    }
    for _ in range(times):
        for record in records:
            await record.callback(dict(envelope))
    return len(records)


__all__ = ["ANY_OBJECT", "deliver_event", "register_event_schemas"]
