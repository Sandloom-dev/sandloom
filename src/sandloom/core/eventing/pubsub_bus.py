"""Redis Pub/Sub event bus (volatile; refused in prod by default)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import (
    Any,
    cast,
)

from sandloom.core.config.settings import settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.contracts.validators import validate_channel_name
from sandloom.core.eventing.bus_base import BaseEventBus
from sandloom.core.eventing.bus_metrics import (
    _event_bus_metrics,
    _warn_if_callback_slow,
)
from sandloom.core.eventing.envelope import (
    CallbackType,
    EventEnvelope,
    _match_channel,
    _now_iso,
)
from sandloom.core.eventing.subscriptions import SubscriptionHandle
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


class RedisEventBus(BaseEventBus):
    """Redis-backed event bus for production.

    Implementation notes:
    - Reconnection with exponential backoff is supported.
    - psubscribe is used to support wildcard channels (prefix.*).
    - When processing messages the ContextVar trace_id is not changed — the
      message is passed directly to the callback.
    """

    def __init__(
        self,
        schema_registry: SchemaRegistryContract,
        *,
        client_factory: Callable[[], Awaitable[Any]] | None = None,
    ):
        super().__init__(schema_registry)
        self._subs: dict[str, list[CallbackType]] = {}
        self._subscription_handles: dict[str, list[SubscriptionHandle]] = {}
        self._task: asyncio.Task[None] | None = None
        self._pubsub: Any | None = None
        self._stopped = False
        self._lock = asyncio.Lock()
        # How this bus instance obtains its Redis client. When the infra driver
        # builds the bus for a NAMED eventbus instance it injects a factory that
        # connects from that instance's options, so named instances + driver
        # options work. ``None`` falls back to the shared default client.
        self._client_factory = client_factory
        # Limit the number of concurrent callbacks to reduce pressure
        # (the value is exposed in settings for configuration)
        self._callback_semaphore = asyncio.Semaphore(
            settings.EVENT_BUS_REDIS_SEMAPHORE_LIMIT
        )

    async def _get_client(self) -> Any:
        if self._client_factory is None:
            # The event-bus driver always injects a factory (a named instance's
            # own client, or the shared default for the ``default`` instance).
            # A bus built without one cannot do I/O — and core must not reach
            # into the driver layer to paper over missing wiring.
            raise RuntimeError(
                "RedisEventBus has no client factory; it must be constructed by "
                "the event-bus driver, which injects one"
            )
        return await self._client_factory()

    @staticmethod
    def _is_pattern(ch: str) -> bool:
        return ch.endswith(".*")

    async def _publish_to_transport(self, channel: str, message: EventEnvelope) -> None:
        try:
            client = await self._get_client()
            # client.publish is expected to return the number of subscribers or None
            await client.publish(channel, json.dumps(message))
        except Exception:
            logger.exception("Failed to publish to Redis channel=%s", channel)
            # Re-raise so callers (outbox relay, post-commit dispatch) see the
            # failure and can retry; swallowing it here would mark undelivered
            # messages as delivered.
            raise

    async def subscribe(
        self, channel: str, callback: CallbackType
    ) -> SubscriptionHandle:
        validate_channel_name(channel)

        async def _close() -> None:
            await self._remove_subscription(channel, callback, handle)

        handle = SubscriptionHandle(_close)
        handle.mark_starting()
        async with self._lock:
            self._subs.setdefault(channel, []).append(callback)
            self._subscription_handles.setdefault(channel, []).append(handle)
            # Create the listen task INSIDE the lock so two concurrent first
            # subscribers cannot both see "no task" and start duplicate loops.
            # create_task is synchronous, so holding the lock here is safe.
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._listen_loop())
                self._task.add_done_callback(self._listen_task_done)
                # A fresh loop subscribes to every channel in _subs (including
                # this one) when it establishes _pubsub — nothing to do here.
                pubsub = None
            else:
                pubsub = self._pubsub
        # pubsub is None either because we just started the loop, or because the
        # running loop has not established its connection yet; in both cases the
        # loop's startup snapshot of _subs (taken under the same lock) will pick
        # this channel up, so a dynamic subscribe is only needed when live.
        try:
            if pubsub is not None:
                if self._is_pattern(channel):
                    await pubsub.psubscribe(channel)
                else:
                    await pubsub.subscribe(channel)
                handle.mark_running()
        except asyncio.CancelledError:
            # Cancellation may arrive after local registration but while the
            # network subscribe is blocked. Roll back by handle identity before
            # propagating so a runtime retry cannot leave a duplicate callback.
            try:
                await self._remove_subscription(
                    channel,
                    callback,
                    handle,
                    notify_transport=False,
                )
            finally:
                handle.mark_stopped()
            raise
        except Exception as exc:  # pragma: no cover
            handle.mark_retrying(exc)
            logger.exception("Failed to dynamically subscribe channel=%s", channel)
            # Force the shared listen loop to reconnect and resubscribe from
            # the complete registry; otherwise this one channel would stay
            # silently absent while the old connection kept running.
            if pubsub is not None:
                try:
                    await pubsub.close()
                except Exception:  # noqa: BLE001, S110 - best-effort close of the replaced pubsub
                    pass
        safe_inc("subscriptions_total", {"channel": channel})
        return handle

    def _listen_task_done(self, task: asyncio.Task[None]) -> None:
        if self._stopped or task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            exc = RuntimeError("Redis listen loop stopped unexpectedly")
        for handles in self._subscription_handles.values():
            for handle in handles:
                handle.mark_failed(exc)

    @staticmethod
    def _subscription_index(
        callbacks: list[CallbackType],
        handles: list[SubscriptionHandle] | None,
        callback: CallbackType,
        handle: SubscriptionHandle | None,
    ) -> int:
        """Position of the subscription to remove (by handle when known)."""
        if handle is not None and handles is not None:
            try:
                return handles.index(handle)
            except ValueError:
                return -1
        try:
            return callbacks.index(callback)
        except ValueError:
            return -1

    async def _remove_subscription(
        self,
        channel: str,
        callback: CallbackType,
        handle: SubscriptionHandle | None = None,
        *,
        notify_transport: bool = True,
    ) -> None:
        to_unsub = False
        async with self._lock:
            callbacks = self._subs.get(channel)
            handles = self._subscription_handles.get(channel)
            if callbacks is not None:
                index = self._subscription_index(callbacks, handles, callback, handle)
                if index >= 0:
                    callbacks.pop(index)
                    if handles is not None and index < len(handles):
                        removed = handles.pop(index)
                        if removed is not handle:
                            removed.mark_stopped()
                if not callbacks:
                    self._subs.pop(channel, None)
                    self._subscription_handles.pop(channel, None)
                    to_unsub = True
            pubsub = self._pubsub
        if notify_transport and to_unsub and pubsub:
            try:
                if self._is_pattern(channel):
                    await pubsub.punsubscribe(channel)
                else:
                    await pubsub.unsubscribe(channel)
            except Exception:  # pragma: no cover
                logger.exception(
                    "Failed to dynamically unsubscribe channel=%s", channel
                )

    def unsubscribe(self, channel: str, callback: CallbackType) -> None:
        async def _do_remove() -> None:
            await self._remove_subscription(channel, callback)

        self._track_task(asyncio.create_task(_do_remove()))

    async def _listen_loop(self) -> None:
        """Long-lived Redis listen loop with reconnection logic."""
        reconnect_delay = 1.0
        max_delay = 32.0

        while not self._stopped:
            try:
                new_pubsub = await self._connect_pubsub()
                # Successful connection — reset the delay
                reconnect_delay = 1.0
                await self._listen(new_pubsub)
                if not self._stopped:
                    raise ConnectionError("Redis pub/sub connection closed")

            except asyncio.CancelledError:
                logger.info("Redis listen loop cancelled")
                break
            except Exception as exc:
                for handles in self._subscription_handles.values():
                    for handle in handles:
                        handle.mark_retrying(exc)
                logger.exception(
                    "Redis listen loop error, will reconnect (delay=%.1f)",
                    reconnect_delay,
                )
                _event_bus_metrics["redis_reconnect_attempts"] += 1
                if self._stopped:
                    break  # type: ignore[unreachable]
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)

        # Teardown: close pubsub if present
        try:
            if self._pubsub:
                await self._pubsub.close()
        except Exception:  # noqa: BLE001, S110 - best-effort pubsub teardown
            pass

    async def _connect_pubsub(self) -> Any:
        """Replace the pubsub connection and subscribe every registered channel."""
        client = await self._get_client()

        # Close the previous pubsub (safely)
        if self._pubsub:
            try:
                await self._pubsub.close()
            except Exception:  # pragma: no cover  # noqa: BLE001, S110 - best-effort close of the replaced pubsub
                pass

        new_pubsub = client.pubsub()
        async with self._lock:
            self._pubsub = new_pubsub
            channels = list(self._subs.keys())
        if channels:
            patterns = [c for c in channels if self._is_pattern(c)]
            normals = [c for c in channels if not self._is_pattern(c)]
            # No subscribe() fallback for patterns: a literal
            # subscription on "prefix.*" would never match real
            # channels and silently drop events.
            if patterns:
                await new_pubsub.psubscribe(*patterns)
            if normals:
                await new_pubsub.subscribe(*normals)
        for handles in self._subscription_handles.values():
            for handle in handles:
                handle.mark_running()

        logger.info("RedisEventBus: listening on channels=%s", channels)
        return new_pubsub

    async def _listen(self, new_pubsub: Any) -> None:
        """Dispatch messages until the connection ends (or the bus stops)."""
        # Listen for messages; the message format depends on the client.
        channel: str = "unknown"
        async for raw_msg in new_pubsub.listen():
            if self._stopped:
                break

            try:
                # raw_msg may be a dict with keys: type, pattern, channel, data
                mtype = raw_msg.get("type")
                # Obtain the channel up front so the variable always exists
                chan_val = raw_msg.get("channel") or raw_msg.get("pattern")
                channel = chan_val if isinstance(chan_val, str) else "unknown"
                if mtype not in ("message", "pmessage"):
                    continue
                envelope = self._pubsub_envelope(channel, raw_msg.get("data"))
                if envelope is None:
                    continue
                await self._fan_out(channel, envelope)

            except Exception:
                logger.exception("Error processing Redis message")
                ch_fail: str = channel
                safe_inc(
                    "events_delivered_total",
                    {"channel": ch_fail, "status": "failure"},
                )

    @staticmethod
    def _pubsub_envelope(channel: str, data: Any) -> EventEnvelope | None:
        """The envelope of one pub/sub message (``None``: malformed, dropped)."""
        # Decode if needed
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode()
            except Exception:  # noqa: BLE001, S110 - undecodable payload stays bytes
                # keep bytes if decoding failed
                pass

        # If data is a JSON string — parse it. Malformed JSON
        # is dropped (with a metric) — wrapping garbage into a
        # fake envelope just moves the failure into callbacks.
        if isinstance(data, str):
            try:
                loaded = json.loads(data)
            except json.JSONDecodeError:
                logger.warning(
                    "Dropping malformed message on channel=%s",
                    channel,
                )
                safe_inc("events_malformed_total", {"channel": channel})
                return None
        else:
            loaded = data

        if isinstance(loaded, dict) and "payload" in loaded and "id" in loaded:
            # Assume this is already a complete envelope
            return cast(EventEnvelope, loaded)
        # Build an envelope from arbitrary data
        if isinstance(loaded, dict):
            payload_inner = cast(dict[str, Any], loaded)
        elif loaded is None:
            payload_inner = {"raw": data}
        else:
            payload_inner = {"data": loaded}
        return EventEnvelope(
            id=str(uuid.uuid4()),
            channel=channel,
            schema_version="v1",
            trace_id=None,
            emitted_at=_now_iso(),
            payload=payload_inner,
        )

    async def _fan_out(self, channel: str, envelope: EventEnvelope) -> None:
        """Start one callback task per matching subscription."""
        # Filter callbacks by subscriptions (prefix.* support)
        async with self._lock:
            callbacks = [
                cb
                for sub, subs in self._subs.items()
                if _match_channel(sub, channel)
                for cb in subs
            ]

        # Run handlers in separate tasks — do not block the loop
        for cb in callbacks:
            self._track_task(
                asyncio.create_task(self._invoke_callback(cb, channel, envelope))
            )

    async def _invoke_callback(
        self, cb: CallbackType, channel: str, message: EventEnvelope
    ) -> None:
        async with self._callback_semaphore:
            try:
                ch = message.get("channel", channel)
                started = time.perf_counter()
                await cb(message)
                _warn_if_callback_slow(channel, time.perf_counter() - started)
                _event_bus_metrics["callbacks_success"] += 1
                safe_inc("events_delivered_total", {"channel": ch, "status": "success"})
            except Exception:
                logger.exception("Callback failed for Redis channel=%s", channel)
                ch = message.get("channel", channel)
                _event_bus_metrics["callbacks_failure"] += 1
                safe_inc("events_delivered_total", {"channel": ch, "status": "failure"})

    async def close(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for handles in self._subscription_handles.values():
            for handle in handles:
                handle.mark_stopped()
        # Let in-flight callbacks finish before tearing down the connection,
        # but bounded — a hung callback must not hang shutdown.
        await self._drain_bg_tasks(timeout=settings.event_shutdown_drain_seconds)
        try:
            if self._pubsub:
                await self._pubsub.close()
        except Exception:  # noqa: BLE001, S110 - best-effort pubsub teardown
            pass
        self._subscription_handles.clear()
