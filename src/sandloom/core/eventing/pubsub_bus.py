"""The volatile event bus over ``broadcast`` (driver ``broadcast``; refused in prod by
default).

The bus speaks no backend: it publishes and subscribes through a ``broadcast``
instance (spec sections 6 and 7). One broadcast subscription serves one bus
channel (a name or a ``prefix.*`` pattern), so a new subscriber never disturbs
the connection of another and no message falls into a gap while a channel is
added. A subscription that ends is a gap: the bus logs the drop, resubscribes
after a backoff and says so on the handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from functools import partial
from typing import (
    Any,
    cast,
)

import sandloom.core.infra.access as access
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
    _now_iso,
)
from sandloom.core.eventing.subscriptions import SubscriptionHandle
from sandloom.core.infra.errors import Unsupported
from sandloom.core.infra.ports.broadcast import Broadcast
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


class BroadcastEventBus(BaseEventBus):
    """The volatile bus over a ``broadcast`` instance.

    - ``broadcast`` is the instance, a callable answering it (looked up at
      each connect, so a provider rebuilt for another event loop is picked up)
      or the NAME of an instance of the active registry; ``None`` is the
      ``default`` instance. The bus holds no client of its own.
    - ``prefix.*`` channels need the instance's ``pattern_subscribe``
      (:meth:`check_broadcast` refuses an instance without it).
    - When processing messages the ContextVar trace_id is not changed: the
      message is passed directly to the callback.
    """

    def __init__(
        self,
        schema_registry: SchemaRegistryContract,
        *,
        broadcast: Broadcast | Callable[[], Broadcast] | str | None = None,
    ):
        super().__init__(schema_registry)
        self._subs: dict[str, list[CallbackType]] = {}
        self._subscription_handles: dict[str, list[SubscriptionHandle]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopped = False
        self._lock = asyncio.Lock()
        self._broadcast_source = broadcast
        # Limit the number of concurrent callbacks to reduce pressure
        # (the value is exposed in settings for configuration)
        self._callback_semaphore = asyncio.Semaphore(
            settings.EVENT_BUS_REDIS_SEMAPHORE_LIMIT
        )

    def _broadcast(self) -> Broadcast:
        source = self._broadcast_source
        if source is None:
            return access.broadcast()
        if isinstance(source, str):
            return access.broadcast(source)
        if callable(source) and not hasattr(source, "publish"):
            return source()
        return cast(Broadcast, source)

    def broadcast_instance(self) -> Broadcast:
        """The ``broadcast`` instance this bus publishes and subscribes through."""
        return self._broadcast()

    def check_broadcast(self) -> None:
        """Refuse a ``broadcast`` instance without ``pattern_subscribe``: the
        bus subscribes to ``prefix.*`` channels."""
        features = getattr(self._broadcast(), "features", None)
        if callable(features) and "pattern_subscribe" not in features():
            raise Unsupported(
                "pattern_subscribe",
                capability="broadcast",
                instance=str(self._broadcast_source or "default"),
            )

    async def _publish_to_transport(self, channel: str, message: EventEnvelope) -> None:
        try:
            await self._broadcast().publish(channel, json.dumps(message).encode())
        except Exception as exc:
            # The error type only, no traceback: a broadcast driver's error text
            # names the host and port (and can carry the payload).
            logger.error(
                "Failed to publish to broadcast channel=%s (%s)",
                channel,
                type(exc).__name__,
            )
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
            # The task is created INSIDE the lock so two concurrent first
            # subscribers of one channel cannot both see "no task".
            task = self._tasks.get(channel)
            if task is None or task.done():
                task = asyncio.create_task(self._run(channel), name=f"bus:{channel}")
                self._tasks[channel] = task
                task.add_done_callback(partial(self._listen_task_done, channel))
        safe_inc("subscriptions_total", {"channel": channel})
        return handle

    def _listen_task_done(self, channel: str, task: asyncio.Task[None]) -> None:
        if self._stopped or task.cancelled():
            return
        exc = task.exception() or RuntimeError("the bus listen loop stopped")
        for handle in self._subscription_handles.get(channel, ()):
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
        task: asyncio.Task[None] | None = None
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
                    task = self._tasks.pop(channel, None)
        if task is not None and notify_transport:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001, S110 - the loop already reported it
                pass

    def unsubscribe(self, channel: str, callback: CallbackType) -> None:
        async def _do_remove() -> None:
            await self._remove_subscription(channel, callback)

        self._track_task(asyncio.create_task(_do_remove()))

    async def _run(self, channel: str) -> None:
        """Serve one bus channel until it has no subscriber or the bus stops:
        subscribe on the broadcast instance, fan its messages out, and when the
        subscription ends (``Unavailable``) treat the interval as a drop and
        subscribe again after a backoff."""
        reconnect_delay = 1.0
        max_delay = 32.0
        while not self._stopped and channel in self._subs:
            subscription: Any | None = None
            try:
                subscription = await self._broadcast().subscribe([channel])
                reconnect_delay = 1.0
                for handle in self._handles(channel):
                    handle.mark_running()
                logger.info("event bus: listening on channel=%s", channel)
                async for message in subscription:
                    await self._deliver(channel, message)
                raise ConnectionError("the broadcast subscription ended")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - logged by type and counted; the loop resubscribes
                for handle in self._handles(channel):
                    handle.mark_retrying(exc)
                # The error type only, no traceback (host, port and payload text).
                logger.warning(
                    "event bus: lost the subscription of channel=%s (%s); messages "
                    "published meanwhile are dropped (volatile bus); "
                    "resubscribing (delay=%.1f)",
                    channel,
                    type(exc).__name__,
                    reconnect_delay,
                )
                _event_bus_metrics["redis_reconnect_attempts"] += 1
                if self._stopped:
                    break  # type: ignore[unreachable]
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)
            finally:
                if subscription is not None:
                    try:
                        await subscription.aclose()
                    except Exception:  # noqa: BLE001, S110 - best-effort teardown
                        pass

    def _handles(self, channel: str) -> list[SubscriptionHandle]:
        return list(self._subscription_handles.get(channel, ()))

    async def _deliver(self, subscribed: str, message: Any) -> None:
        """Dispatch one broadcast message to the callbacks of ``subscribed``."""
        try:
            envelope = self._pubsub_envelope(message.channel, message.payload)
            if envelope is None:
                return
            await self._fan_out(subscribed, message.channel, envelope)
        except Exception as exc:  # noqa: BLE001 - logged by type and counted; one message must not stop the loop
            # The error type only, no traceback (host, port and payload text).
            logger.error("Error processing broadcast message (%s)", type(exc).__name__)
            safe_inc(
                "events_delivered_total",
                {
                    "channel": str(getattr(message, "channel", subscribed)),
                    "status": "failure",
                },
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

    async def _fan_out(
        self, subscribed: str, channel: str, envelope: EventEnvelope
    ) -> None:
        """Start one callback task per callback subscribed as ``subscribed``
        (a name or a ``prefix.*`` pattern; the message came on ``channel``)."""
        async with self._lock:
            callbacks = list(self._subs.get(subscribed, ()))

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
            except Exception as exc:
                # Callbacks are application code: the full traceback is what
                # finds the bug (the driver and transport branches log the type
                # only).
                logger.error(
                    "Callback failed for broadcast channel=%s (%s)",
                    channel,
                    type(exc).__name__,
                    exc_info=True,
                )
                ch = message.get("channel", channel)
                _event_bus_metrics["callbacks_failure"] += 1
                safe_inc("events_delivered_total", {"channel": ch, "status": "failure"})

    async def close(self) -> None:
        self._stopped = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001, S110 - the loop already reported it
                pass
        for handles in self._subscription_handles.values():
            for handle in handles:
                handle.mark_stopped()
        # Let in-flight callbacks finish, but bounded — a hung callback must
        # not hang shutdown.
        await self._drain_bg_tasks(timeout=settings.event_shutdown_drain_seconds)
        self._subscription_handles.clear()
