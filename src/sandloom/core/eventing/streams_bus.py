"""Durable event bus over a :class:`MessageTransport` (Redis Streams)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Sequence
from functools import partial
from typing import (
    Any,
)

from sandloom.core.config.settings import settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.contracts.validators import validate_channel_name
from sandloom.core.eventing.bus_metrics import _event_bus_metrics
from sandloom.core.eventing.consumer_inbox import ConsumerInbox
from sandloom.core.eventing.delivery import stream_start_id
from sandloom.core.eventing.envelope import (
    PREFLIGHTED,
    BatchPublishResult,
    CallbackType,
    EventEnvelope,
    OutboundEvent,
    Prevalidated,
    _now_iso,
    _safe_trace_id,
)
from sandloom.core.eventing.streams_dispatch import _StreamsDispatch
from sandloom.core.eventing.subscriptions import SubscriptionHandle, SubscriptionState
from sandloom.core.eventing.transport_base import (
    TransportBatchError,
    process_consumer_id,
)
from sandloom.core.eventing.transport_capabilities import (
    TransportCapabilities,
    transport_capabilities,
)
from sandloom.core.eventing.transport_capabilities import TransportCapability as Cap
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


def _default_consumer_group(callback: CallbackType) -> str:
    """Derive a stable consumer-group name from a callback's identity.

    Each distinct handler gets its own group so every handler receives every
    event (fan-out), while a crashed handler's unacked messages are redelivered
    to that same group.
    """
    module = getattr(callback, "__module__", "anon")
    qualname = getattr(callback, "__qualname__", getattr(callback, "__name__", "cb"))
    return f"{module}.{qualname}"


class StreamsEventBus(_StreamsDispatch):
    """Durable event bus over a :class:`MessageTransport` (at-least-once).

    Unlike Pub/Sub, ``publish`` appends to a durable per-channel stream, so an
    event emitted while a consumer is offline is delivered when it returns. Each
    subscriber runs a consumer-group loop with explicit ack and id-dedupe
    (:class:`ConsumerInbox`): a handler that raises leaves the message unacked
    for redelivery; a successful handler acks it.

    Channels must be concrete (no ``prefix.*`` wildcard — a stream is addressed
    by exact name).
    """

    durable = True

    _POLL_INTERVAL_SECONDS = 0.5
    # Server-side wait (XREADGROUP BLOCK) per idle consume; bounded so the loop
    # notices stop/unsubscribe and due retries promptly.
    _BLOCK_MS = 2_000
    # Subscription-inventory heartbeat + lag/pending gauge refresh interval.
    _HEARTBEAT_SECONDS = 30.0

    def __init__(
        self,
        schema_registry: SchemaRegistryContract,
        transport: Any,
        *,
        poll_interval: float | None = None,
        block_ms: int | None = None,
        read_count: int | None = None,
    ) -> None:
        super().__init__(schema_registry)
        self._transport = transport
        # Distinguishes several buses inside ONE process (tests, embedded
        # peers): each is its own Streams consumer, so one never re-claims
        # entries another is still handling as if they were its own.
        self._instance_id = uuid.uuid4().hex[:8]
        self._read_count = max(
            1,
            int(
                read_count
                if read_count is not None
                else getattr(settings, "event_stream_read_count", 64) or 64
            ),
        )
        # (channel, group) -> callbacks; one consumer loop per (channel, group)
        self._subs: dict[tuple[str, str], list[CallbackType]] = {}
        self._subscription_handles: dict[tuple[str, str], list[SubscriptionHandle]] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._stopped = False
        self._lock = asyncio.Lock()
        self._poll = (
            poll_interval if poll_interval is not None else self._POLL_INTERVAL_SECONDS
        )
        if block_ms is not None:
            self._block_ms = block_ms
        elif poll_interval is not None:
            self._block_ms = max(1, int(poll_interval * 1000))
        else:
            self._block_ms = self._BLOCK_MS
        # What the transport can do, resolved once (declared by its class,
        # else probed from its methods: see ``transport_capabilities``).
        self._capabilities = transport_capabilities(transport)
        # (channel, group) -> inventory info heartbeated by the consume loop
        self._inventory_info: dict[tuple[str, str], dict[str, Any]] = {}
        # (channel, group) -> last observed {"lag", "pending", "observed_at"}
        self._group_stats: dict[tuple[str, str], dict[str, Any]] = {}
        # channel -> monotonic time of this process's last publish / audit
        self._last_published: dict[str, float] = {}
        self._audience_checked: dict[str, float] = {}

    @property
    def transport(self) -> Any:
        """The durable transport (operator tooling: DLQ inspection/replay)."""
        return self._transport

    @property
    def transport_capabilities(self) -> TransportCapabilities:
        """The optional transport features this bus uses."""
        return self._capabilities

    @staticmethod
    def _is_pattern(channel: str) -> bool:
        return channel.endswith(".*")

    @property
    def native_batch_publish(self) -> bool:
        """True when the transport appends a batch in one round trip.

        ``MessageTransport.publish_many``'s default is a loop over
        ``publish``: only an override (Redis Streams) counts as native.
        """
        return Cap.NATIVE_BATCH_PUBLISH in self._capabilities

    async def _publish_to_transport(self, channel: str, message: EventEnvelope) -> None:
        await self._transport.publish(channel, message)
        self._last_published[channel] = time.monotonic()
        self._schedule_audience_check(channel)

    async def publish_event_batch(
        self, events: Sequence[OutboundEvent], *, prevalidated: Prevalidated = False
    ) -> BatchPublishResult:
        """Publish ``events`` in ONE transport round trip (``publish_many``).

        Envelopes and checks are exactly :meth:`publish_event`'s; an event
        failing its check stops the batch there (the events before it are
        still published, in order). The transport appends the batch atomically
        and in order (Redis Streams: ``MULTI``/``EXEC``); a transport failure
        reports only the leading events it confirmed. A transport without
        ``publish_many`` falls back to the sequential default.
        """
        if Cap.BATCH_PUBLISH not in self._capabilities:
            return await super().publish_event_batch(events, prevalidated=prevalidated)
        items, error = self._prepare_batch(events, prevalidated)
        published = 0
        if items:
            try:
                await self._transport.publish_many(items)
                published = len(items)
            except TransportBatchError as exc:
                published = min(len(exc.published), len(items))
                error = exc
            except Exception as exc:  # noqa: BLE001 - third-party transport
                error = exc
        self._record_batch_publish(events, items, published, error)
        return BatchPublishResult(published, error)

    def _prepare_batch(
        self, events: Sequence[OutboundEvent], prevalidated: Prevalidated
    ) -> tuple[list[tuple[str, EventEnvelope]], BaseException | None]:
        """Envelopes of the leading events that pass their checks (+ the error)."""
        trace_id = _safe_trace_id()
        emitted_at = _now_iso()
        items: list[tuple[str, EventEnvelope]] = []
        error: BaseException | None = None
        for event in events:
            try:
                envelope = self._build_envelope(
                    event.channel,
                    event.payload,
                    event.schema_version,
                    event.event_id,
                    trace_id,
                    emitted_at,
                )
                if not prevalidated:
                    self.preflight(event.channel, event.payload)
                elif prevalidated != PREFLIGHTED:
                    self._check_prevalidated(event.channel, event.payload)
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                error = exc
                break
            items.append((event.channel, envelope))
        return items, error

    def _record_batch_publish(
        self,
        events: Sequence[OutboundEvent],
        items: list[tuple[str, EventEnvelope]],
        published: int,
        error: BaseException | None,
    ) -> None:
        """Count a batch publish and note its channels for the audience check."""
        if error is not None:
            _event_bus_metrics["events_publish_failures"] += 1
            failed_channel = events[min(published, len(events) - 1)].channel
            safe_inc("events_publish_failures_total", {"channel": failed_channel})
        _event_bus_metrics["events_published"] += published
        for channel, _envelope in items[:published]:
            safe_inc("events_published_total", {"channel": channel})
        if published:
            now = time.monotonic()
            for channel in dict.fromkeys(ch for ch, _env in items[:published]):
                self._last_published[channel] = now
                self._schedule_audience_check(channel)

    async def _ensure_group(self, channel: str, group: str, start_from: Any) -> None:
        kwargs: dict[str, Any] = {}
        if Cap.GROUP_START_ID in self._capabilities:
            kwargs["start_id"] = stream_start_id(start_from)
        await self._transport.ensure_group(channel, group, **kwargs)

    async def subscribe(
        self,
        channel: str,
        callback: CallbackType,
        *,
        group: str | None = None,
        start_from: Any = None,
        owner: dict[str, Any] | None = None,
    ) -> SubscriptionHandle:
        """Join ``channel`` under the durable consumer ``group``.

        ``owner`` (the module / database fingerprint) is checked against the
        group's recorded owner first -- a different deployment reusing the group
        raises :class:`~sandloom.core.eventing.transport_base.GroupOwnershipError`.
        ``start_from`` only affects a group being CREATED
        (see ``MessageTransport.ensure_group``).
        """
        if self._is_pattern(channel):
            raise ValueError(
                "StreamsEventBus does not support wildcard subscriptions; "
                "subscribe to concrete versioned channels"
            )
        validate_channel_name(channel)
        grp = group or _default_consumer_group(callback)
        if owner is not None and Cap.GROUP_OWNERSHIP in self._capabilities:
            await self._transport.claim_group_owner(channel, grp, owner)
        await self._ensure_group(channel, grp, start_from)
        if (
            getattr(callback, "sandloom_never_dead_letter", False)
            and Cap.NEVER_DEAD_LETTER in self._capabilities
        ):
            self._transport.never_dead_letter(channel, grp)
        key = (channel, grp)
        self._inventory_info[key] = {**(owner or {}), "channel": channel}
        await self._heartbeat(channel, grp)

        handle = SubscriptionHandle(partial(self._close_subscription, key, callback))
        handle.mark_starting()
        async with self._lock:
            self._subs.setdefault(key, []).append(callback)
            self._subscription_handles.setdefault(key, []).append(handle)
            if key not in self._tasks or self._tasks[key].done():
                task = asyncio.create_task(self._consume_loop(channel, grp))
                self._tasks[key] = task
                task.add_done_callback(partial(self._consume_task_done, key))
        handle.mark_running()
        safe_inc("subscriptions_total", {"channel": channel})
        return handle

    async def _close_subscription(
        self, key: tuple[str, str], callback: CallbackType
    ) -> None:
        """A subscription handle's close: drop it, stop an orphaned loop."""
        task: asyncio.Task[None] | None = None
        async with self._lock:
            callbacks = self._subs.get(key)
            handles = self._subscription_handles.get(key)
            if callbacks is not None:
                try:
                    index = callbacks.index(callback)
                except ValueError:
                    index = -1
                if index >= 0:
                    callbacks.pop(index)
                    if handles is not None and index < len(handles):
                        handles.pop(index)
            if callbacks == []:
                self._subs.pop(key, None)
                self._subscription_handles.pop(key, None)
                task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _consume_task_done(
        self, key: tuple[str, str], task: asyncio.Task[None]
    ) -> None:
        if self._stopped or task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            exc = RuntimeError("consumer loop stopped unexpectedly")
        for handle in self._subscription_handles.get(key, []):
            handle.mark_failed(exc)

    def unsubscribe(self, channel: str, callback: CallbackType) -> None:
        # Sync method: in asyncio it runs to completion without an await point,
        # so the dict/list mutations are atomic w.r.t. other coroutines (no lock
        # needed). When the last callback for a (channel, group) goes, cancel its
        # consume loop and drop its bookkeeping — otherwise the loop polls the
        # transport forever (a leak).
        for key in list(self._subs.keys()):
            if key[0] != channel:
                continue
            try:
                index = self._subs[key].index(callback)
            except ValueError:
                continue
            self._subs[key].pop(index)
            handles = self._subscription_handles.get(key)
            if handles is not None and index < len(handles):
                handles.pop(index).mark_stopped()
            if not self._subs[key]:
                del self._subs[key]
                self._subscription_handles.pop(key, None)
                task = self._tasks.pop(key, None)
                if task is not None and not task.done():
                    task.cancel()

    async def _consume_loop(self, channel: str, group: str) -> None:
        inbox = ConsumerInbox(f"{channel}:{group}")
        consumer = self._register_consumer_name(
            channel, group, f"{group}.{process_consumer_id()}.{self._instance_id}"
        )
        last_heartbeat = time.monotonic()
        failures = 0
        lease = self._batch_lease(channel, group, consumer)
        blocking = Cap.BLOCKING_CONSUME in self._capabilities
        while not self._stopped:
            if time.monotonic() - last_heartbeat >= self._HEARTBEAT_SECONDS:
                last_heartbeat = time.monotonic()
                await self._heartbeat(channel, group)
            try:
                deliveries = await self._read_deliveries(
                    channel, group, consumer, blocking
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Streams consume failed channel=%s", channel)
                self._mark_retrying((channel, group), exc)
                failures += 1
                if await self._sleep_or_stop(self._reconnect_delay(failures)):
                    break
                continue
            self._mark_running((channel, group))
            if not deliveries:
                failures = 0
                # A blocking consume already waited server-side; only a
                # non-blocking transport needs a client-side sleep.
                if not blocking and await self._sleep_or_stop():
                    break
                continue
            try:
                await self._dispatch_with_lease(
                    channel, group, deliveries, inbox, consumer, lease
                )
                failures = 0
            except Exception as exc:
                # Transport-level inbox/ack failures must not silently kill the
                # consumer task. Leave the delivery unacked and reconnect.
                logger.exception("Streams dispatch failed channel=%s", channel)
                self._mark_retrying((channel, group), exc)
                failures += 1
                if await self._sleep_or_stop(self._reconnect_delay(failures)):
                    break

    def _register_consumer_name(self, channel: str, group: str, consumer: str) -> str:
        """Record the loop's consumer name in its inventory entry."""
        # Unique per process so Redis tracks pending per replica and a crashed
        # consumer's backlog can be reclaimed (XAUTOCLAIM) rather than stranded.
        info = self._inventory_info.get((channel, group))
        if info is not None:
            info["consumer"] = consumer
        return consumer

    async def _read_deliveries(
        self, channel: str, group: str, consumer: str, blocking: bool
    ) -> list[Any]:
        """One read of the group's due and new deliveries."""
        deliveries: list[Any]
        if blocking:
            # Server-side wait for new entries (XREADGROUP BLOCK) instead
            # of busy polling; failed entries are redelivered by the
            # transport only after their retry backoff.
            deliveries = await self._transport.consume(
                channel,
                group=group,
                consumer=consumer,
                count=self._read_count,
                block_ms=self._block_ms,
            )
        else:  # pragma: no cover - third-party transport without block
            deliveries = await self._transport.consume(
                channel, group=group, consumer=consumer, count=self._read_count
            )
        return deliveries

    def _mark_retrying(self, key: tuple[str, str], exc: BaseException) -> None:
        for handle in self._subscription_handles.get(key, []):
            handle.mark_retrying(exc)

    def _mark_running(self, key: tuple[str, str]) -> None:
        for handle in self._subscription_handles.get(key, []):
            if handle.state is not SubscriptionState.RUNNING:
                handle.mark_running()

    @staticmethod
    def _reconnect_delay(failures: int) -> float:
        base = settings.event_streams_reconnect_base_seconds
        cap = settings.event_streams_reconnect_max_seconds
        delay = min(cap, base * 2 ** min(max(failures - 1, 0), 30))
        return random.uniform(delay / 2, delay)  # noqa: S311 - backoff jitter, not a secret

    async def _sleep_or_stop(self, delay: float | None = None) -> bool:
        """Sleep with cancellation; failure callers supply jittered backoff."""
        try:
            await asyncio.sleep(self._poll if delay is None else delay)
            return False
        except asyncio.CancelledError:
            return True

    async def close(self) -> None:
        self._stopped = True
        tasks = list(self._tasks.values())
        # Diagnostics-only background checks: cancel, never wait on them.
        tasks.extend(self._bg_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - shutdown awaits every task
                pass
        handles = [
            handle
            for group_handles in self._subscription_handles.values()
            for handle in group_handles
        ]
        for handle in handles:
            handle.mark_stopped()
        async with self._lock:
            self._subs.clear()
            self._subscription_handles.clear()
            self._tasks.clear()
