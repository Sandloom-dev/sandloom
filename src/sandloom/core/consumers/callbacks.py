"""Durable event consumers — the sanctioned "react to a domain event with a
side effect in your own Unit of Work" primitive.

A business module cannot import another module, so when module B must *do
something* in response to module A's event (send a notification, provision a
resource, advance a workflow), it registers an **event consumer** through
``ModuleAPI.register_event_consumer``. The framework subscribes it to the bus
under its OWN durable consumer group and wraps the handler with the concerns an
event handler must ALWAYS have, so individual handlers cannot get them wrong:

* opens a Unit of Work on the consumer's database (commit on success; rollback —
  and therefore at-least-once redelivery — on any exception);
* claims the event id in that same transaction for exactly-once apply
  (``durable`` consumers), turning at-least-once delivery into once-per-effect;
* emits per-consumer metrics and a structured log line (incl. delivery lag).

The cross-module query/projection consumer (``sandloom.core.projections``) is a
SPECIALISATION of this primitive — same wrapper, plus the projection-registry
linkage and storage resolution. Both are subscribed at startup; subscription is
driven by which modules loaded, so it is automatically deploy-correct.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sandloom.core.eventing.delivery import (
    START_FROM_EARLIEST,
    stream_start_id,
)
from sandloom.core.observability.metrics import (
    event_consumer_handler_duration_seconds,
    event_end_to_end_latency_seconds,
)
from sandloom.core.observability.metrics_utils import safe_inc
from sandloom.core.sql_alchemy.durable_inbox import durable_inbox

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession
import sandloom.core.tenant_context as _tenant_context
from sandloom.core.config.settings import settings

logger = logging.getLogger("sandloom.core.eventing.consumers")

# A delivered domain event as it reaches a handler. Re-exported from the public
# surfaces (sandloom.core.di, sandloom.core.projections) so a business module never
# imports the event-bus internals just to type its handler.
EventEnvelope = Mapping[str, Any]

# A handler applies one delivered event to its own UoW session and returns;
# raising triggers redelivery (at-least-once) instead of a silent drop.
EventHandler = Callable[["AsyncSession", EventEnvelope], Awaitable[None]]

# Opens a UoW-managed session for a named database; matches ``di.db_session``.
SessionOpener = Callable[[str], "AbstractAsyncContextManager[AsyncSession]"]


# Characters that would break a Redis key / CLI listing if put in a group name.
_GROUP_FORBIDDEN = frozenset(" \t\r\n|")


def consumer_group_name(name: str, module: str | None, group: str | None) -> str:
    """The durable consumer-group id (and inbox key) of a consumer.

    An explicit ``group`` wins (an operator pin, e.g. to keep a pre-0.4 group).
    Otherwise the group is namespaced by the registering module --
    ``<module>:<name>`` -- so two modules (or the same module deployed in two
    services) choosing the same ``name`` never share one group and split each
    other's events. A consumer registered directly on a bare facade (no owning
    module: tests, scripts) keeps the bare ``name``.
    """
    if group:
        return group
    if module:
        return f"{module}:{name}"
    return name


def validate_consumer_identity(
    kind: str,
    name: str,
    group: str | None,
    start_from: Any,
    bind_tenant_from: str | None,
) -> None:
    """Shared validation of the group / start / tenant-binding options."""
    if group is not None and (not group or _GROUP_FORBIDDEN & set(group)):
        raise ValueError(f"{kind} {name!r}: invalid group {group!r}")
    if _GROUP_FORBIDDEN & set(name):
        raise ValueError(f"{kind} {name!r}: name must not contain whitespace or '|'")
    stream_start_id(start_from)  # raises ValueError on a bad value
    if bind_tenant_from is not None and not bind_tenant_from:
        raise ValueError(f"{kind} {name!r}: bind_tenant_from must be non-empty")


@dataclass(frozen=True, slots=True)
class EventConsumerDescriptor:
    """A durable consumer of one event channel, applied in its own UoW.

    The durable consumer-group id on the bus is :attr:`group_name` --
    ``<module>:<name>`` when registered through ``ModuleAPI`` (``module`` is
    stamped by the loader), the bare ``name`` on a bare facade, or an explicit
    ``group`` pin. Each consumer gets its own pending list, redelivery and
    dead-letter queue; the same id keys its exactly-once inbox. Keep ``name``
    stable across deploys. ``durable`` (default) claims each event id for
    exactly-once apply; idempotent handlers may set it ``False``. ``database``
    is the logical instance whose UoW the handler runs in. A ``required``
    consumer must have an active subscription before the process becomes ready
    and keeps readiness fail-closed if its consume loop later becomes
    unavailable. ``dead_letter=False`` exempts the consumer from delivery-count
    dead-lettering: a failing delivery is retried FOREVER with the transport's
    capped backoff (security effects that must never be silently parked); only
    a :class:`PoisonEventError` dead-letters one.

    ``start_from`` only matters when the group is CREATED: ``"earliest"``
    (default -- every event still within stream retention), ``"latest"`` (only
    new events) or a timestamp. ``bind_tenant_from`` (opt-in) names a payload
    field holding the tenant id; the handler then runs with that tenant bound so
    ``TenantScoped`` reads/writes work (a missing/invalid value fails the
    delivery instead of running unscoped).
    """

    name: str
    channel: str
    handler: EventHandler
    durable: bool = True
    database: str = "default"
    required: bool = True
    module: str | None = None
    group: str | None = None
    start_from: str = START_FROM_EARLIEST
    bind_tenant_from: str | None = None
    dead_letter: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("EventConsumerDescriptor.name must be non-empty")
        if not self.channel:
            raise ValueError(f"event consumer {self.name!r}: channel must be non-empty")
        if not callable(self.handler):
            raise TypeError(f"event consumer {self.name!r}: handler must be callable")
        validate_consumer_identity(
            "event consumer",
            self.name,
            self.group,
            self.start_from,
            self.bind_tenant_from,
        )

    @property
    def group_name(self) -> str:
        """The durable consumer-group id (also the exactly-once inbox key)."""
        return consumer_group_name(self.name, self.module, self.group)


def event_lag_seconds(envelope: EventEnvelope) -> float | None:
    """Seconds between the event being emitted and now, if derivable."""
    emitted = envelope.get("emitted_at")
    if not isinstance(emitted, str):
        return None
    try:
        ts = datetime.fromisoformat(emitted.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds())


def build_consumer_callback(
    *,
    name: str,
    database: str,
    handler: EventHandler,
    durable: bool,
    opener: SessionOpener,
    metric_processed: str,
    metric_failed: str,
    labels: dict[str, str],
    bind_tenant_from: str | None = None,
    dead_letter: bool = True,
) -> Callable[[EventEnvelope], Any]:
    """Wrap a handler into an event-bus callback with UoW + exactly-once + metrics.

    Shared by the generic event-consumer path and the projection-consumer
    specialisation — only the metric names and labels differ.
    """

    async def _apply(envelope: EventEnvelope, event_id: str) -> bool:
        async with opener(database) as session:
            if durable and event_id:
                claimed = await durable_inbox(name).claim(session, event_id)
                if not claimed:
                    return False
            await handler(session, envelope)
        return True

    # Children resolved once per consumer (``labels["consumer"]`` is its name).
    consumer_label = labels.get("consumer", name)
    end_to_end = event_end_to_end_latency_seconds.labels(consumer_label)
    handler_duration = event_consumer_handler_duration_seconds.labels(consumer_label)

    async def _callback(envelope: EventEnvelope) -> None:
        event_id = str(envelope.get("id", "") or "")
        started = time.perf_counter()
        try:
            with _tenant_scope(envelope, bind_tenant_from, name):
                applied = await _apply(envelope, event_id)
            if not applied:
                safe_inc(metric_processed, {**labels, "result": "skipped"})
                return
        except Exception:
            # Leave the message unacked: the bus redelivers it (at-least-once)
            # and dead-letters it once the delivery budget is exhausted.
            safe_inc(metric_failed, labels)
            logger.exception(
                "event consumer failed consumer=%s event=%s", name, event_id
            )
            raise
        duration = time.perf_counter() - started
        lag = event_lag_seconds(envelope)
        safe_inc(metric_processed, {**labels, "result": "applied"})
        handler_duration.observe(duration)
        if lag is not None:
            end_to_end.observe(lag)
        # Per-event success line at DEBUG (the metrics above carry the rates
        # and latencies); failures stay logged at ERROR with the traceback.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "event.consumed",
                extra={
                    "consumer": name,
                    "event_id": event_id,
                    "handler_duration_seconds": round(duration, 6),
                    "event_lag_seconds": None if lag is None else round(lag, 3),
                },
            )

    # Read by the buses at subscribe time (feature flag, no signature change).
    _callback.sandloom_never_dead_letter = not dead_letter  # type: ignore[attr-defined]
    # The DB claim in the handler's own UoW is this consumer's dedupe: a bus
    # may skip its best-effort cache inbox for a group of durable callbacks.
    _callback.sandloom_durable = durable  # type: ignore[attr-defined]
    return _callback


@contextmanager
def _tenant_scope(
    envelope: EventEnvelope, field: str | None, consumer: str
) -> Iterator[None]:
    """Bind the tenant named by ``envelope.payload[field]`` (opt-in).

    Fail-closed: when the consumer opted in but the payload carries no valid
    tenant id, the delivery fails (retried, then dead-lettered) rather than
    running the handler unscoped.
    """
    if field is None:
        yield
        return

    payload = envelope.get("payload")
    raw = payload.get(field) if isinstance(payload, Mapping) else None
    tenant_id: uuid.UUID | None
    try:
        tenant_id = uuid.UUID(str(raw)) if raw is not None else None
    except ValueError:
        tenant_id = None
    if tenant_id is None:
        raise ValueError(
            f"event consumer {consumer!r}: payload field {field!r} holds no "
            f"tenant id (bind_tenant_from) in event {envelope.get('id')!r}"
        )
    with _tenant_context.bind_tenant(tenant_id):
        yield


class VolatileBusError(ValueError):
    """A durable consumer was registered on a bus that cannot honour it.

    A ``ValueError`` so the consumer runtime treats it as permanent (no retry):
    a required consumer then fails startup instead of silently degrading.
    """


def _allow_volatile_consumers() -> bool:
    # SANDLOOM_ACCEPTED_RISKS "volatile_event_bus"; only consulted outside prod.
    return settings.accepts_risk("volatile_event_bus")


def _check_bus_can_host_consumer(bus: Any, channel: str, group: str) -> None:
    """Refuse a consumer-group subscription the bus would silently degrade.

    Registered consumers are DURABLE by contract (own consumer group, ack,
    redelivery, DLQ). A bus without consumer groups drops ``group``:

    * the in-process memory bus (dev/tests) is accepted outside prod — there is
      no other process to miss events or duplicate work;
    * a cross-process volatile bus (Redis Pub/Sub) loses events published while
      the consumer is offline and fans every event out to every replica, so it
      is refused unless the risk ``volatile_event_bus`` is accepted (non-prod);
    * in production any non-durable bus is refused.
    """

    if getattr(bus, "durable", False):
        return
    in_process = bool(getattr(bus, "in_process", False))
    if settings.is_prod:
        reason = "production requires a durable event bus (EVENT_BUS_BACKEND=streams)"
    elif in_process:
        return
    elif _allow_volatile_consumers():
        logger.warning(
            "consumer %s on %s runs on a VOLATILE bus without consumer groups "
            "(accepted risk volatile_event_bus): events may be lost or handled "
            "by every replica",
            group,
            channel,
        )
        return
    else:
        reason = (
            "the bus has no consumer groups/persistence (Redis Pub/Sub); use "
            "EVENT_BUS_BACKEND=streams or accept lossy delivery outside "
            'production with SANDLOOM_ACCEPTED_RISKS=["volatile_event_bus"]'
        )
    raise VolatileBusError(
        f"durable consumer {group!r} on {channel!r} cannot run on "
        f"{type(bus).__name__}: {reason}"
    )


def _side_effect_callback(
    descriptor: Any, database: str, opener: SessionOpener, group: str
) -> Any:
    """A generic (side-effect) consumer's :func:`build_consumer_callback`."""
    return build_consumer_callback(
        name=group,
        database=database,
        handler=descriptor.handler,
        durable=descriptor.durable,
        opener=opener,
        metric_processed="event_consumer_events_processed_total",
        metric_failed="event_consumer_events_failed_total",
        labels={"consumer": group},
        bind_tenant_from=getattr(descriptor, "bind_tenant_from", None),
        dead_letter=getattr(descriptor, "dead_letter", True),
    )


def _projection_callback(
    descriptor: Any, database: str, opener: SessionOpener, group: str
) -> Any:
    """A projection consumer's :func:`build_consumer_callback`.

    ``database`` is the projection's storage instance. Used by the supervised
    runtime and by ``sandloom.core.projections.make_consumer_callback``.
    """
    return build_consumer_callback(
        name=group,
        database=database,
        handler=descriptor.handler,
        durable=descriptor.durable,
        opener=opener,
        metric_processed="projection_events_processed_total",
        metric_failed="projection_events_failed_total",
        labels={"projection": descriptor.projection, "consumer": group},
        bind_tenant_from=getattr(descriptor, "bind_tenant_from", None),
    )


async def subscribe_to_bus(
    bus: Any,
    channel: str,
    callback: Any,
    group: str,
    **options: Any,
) -> Any:
    """Subscribe, passing the durable ``group`` only to buses that accept it.

    The durable :class:`StreamsEventBus` takes a ``group`` (one consumer group
    per consumer); the in-memory bus used in dev/tests does not — so we
    feature-detect rather than couple to a concrete bus type. A bus that cannot
    honour the group is refused (see :func:`_check_bus_can_host_consumer`)
    instead of the group being silently dropped.

    ``options`` (``start_from``, ``owner``) are durable-group
    metadata: each is forwarded only when the bus's ``subscribe`` declares it,
    so third-party / test buses with the minimal signature keep working.
    """
    try:
        params = inspect.signature(bus.subscribe).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        params = {}  # type: ignore[assignment]
    if "group" in params:
        extra = {
            key: value
            for key, value in options.items()
            if value is not None and key in params
        }
        return await bus.subscribe(channel, callback, group=group, **extra)
    _check_bus_can_host_consumer(bus, channel, group)
    return await bus.subscribe(channel, callback)


__all__ = [
    "EventEnvelope",
    "EventHandler",
    "SessionOpener",
    "EventConsumerDescriptor",
    "consumer_group_name",
    "validate_consumer_identity",
    "event_lag_seconds",
    "build_consumer_callback",
    "subscribe_to_bus",
    "VolatileBusError",
]
