"""In-memory event bus (tests / local development)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time

from sandloom.core.config.settings import settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.contracts.validators import validate_channel_name
from sandloom.core.eventing.bus_base import BaseEventBus
from sandloom.core.eventing.bus_metrics import (
    _event_bus_metrics,
    _warn_if_callback_slow,
)
from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.eventing.envelope import CallbackType, EventEnvelope, _match_channel
from sandloom.core.eventing.subscriptions import SubscriptionHandle
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


class InMemoryEventBus(BaseEventBus):
    """In-memory event bus for tests and local development.

    Delivery mirrors the durable transport's semantics rather than running
    subscribers inline inside ``publish``:

    * ``publish`` only ENQUEUES: each subscriber receives the event in its own
      task, started in a FRESH ``contextvars.Context`` — a consumer never
      inherits the publisher's request-scoped state (bound tenant, UoW session,
      request id), exactly like a consumer in another process;
    * a failing subscriber is retried with backoff up to ``max_deliveries``
      times, then dead-lettered (:attr:`dead_letters`, logged) — never silently
      swallowed while the outbox row is marked delivered;
    * handlers are not subject to the publisher's timeouts (the post-commit
      dispatch budget can no longer cancel a long handler).

    Tests that assert on side effects ``await bus.drain()`` first.
    """

    #: Delivery guarantees (feature-detected by the consumer runtime): not
    #: durable, but in-process — no other process can miss events.
    durable = False
    in_process = True

    DEFAULT_MAX_DELIVERIES = 5
    DEFAULT_RETRY_BASE_SECONDS = 0.05
    DEFAULT_RETRY_MAX_SECONDS = 2.0

    def __init__(
        self,
        schema_registry: SchemaRegistryContract,
        *,
        max_deliveries: int | None = None,
        retry_base_seconds: float | None = None,
        retry_max_seconds: float | None = None,
    ):
        super().__init__(schema_registry)
        self._subs: dict[str, list[CallbackType]] = {}
        self._subscription_handles: dict[str, list[SubscriptionHandle]] = {}
        # Guard against concurrent subscribe/unsubscribe and publishes
        self._lock = asyncio.Lock()
        self._max_deliveries = max(
            1,
            max_deliveries
            if max_deliveries is not None
            else self.DEFAULT_MAX_DELIVERIES,
        )
        self._retry_base = (
            retry_base_seconds
            if retry_base_seconds is not None
            else self.DEFAULT_RETRY_BASE_SECONDS
        )
        self._retry_max = (
            retry_max_seconds
            if retry_max_seconds is not None
            else self.DEFAULT_RETRY_MAX_SECONDS
        )
        self._deliveries: set[asyncio.Task[None]] = set()
        #: Events whose subscriber kept failing: (channel, envelope, attempts).
        self.dead_letters: list[tuple[str, EventEnvelope, int]] = []

    async def _publish_to_transport(self, channel: str, message: EventEnvelope) -> None:
        # Take a snapshot of subscriptions under the lock to avoid races
        async with self._lock:
            callbacks: list[CallbackType] = [
                cb
                for sub, subs in self._subs.items()
                if _match_channel(sub, channel)
                for cb in subs
            ]
        if not callbacks:
            return
        loop = asyncio.get_running_loop()
        for cb in callbacks:
            # ``create_task`` copies the CURRENT context; running it inside an
            # empty Context gives the delivery a clean one (no request-scoped
            # contextvars leak into the consumer).
            task = contextvars.Context().run(
                loop.create_task, self._deliver(cb, channel, message)
            )
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)

    async def _deliver(
        self, cb: CallbackType, channel: str, message: EventEnvelope
    ) -> None:
        ch = message.get("channel", channel)
        forever = bool(getattr(cb, "sandloom_never_dead_letter", False))
        attempt = 0
        while True:
            attempt += 1
            try:
                started = time.perf_counter()
                await cb(message)
            except asyncio.CancelledError:
                raise
            except PoisonEventError as exc:
                _event_bus_metrics["callbacks_failure"] += 1
                logger.critical(
                    "In-memory delivery dead-lettered as poison (%s) channel=%s "
                    "event=%s",
                    exc,
                    channel,
                    message.get("id"),
                )
                self.dead_letters.append((channel, message, attempt))
                safe_inc("streams_messages_dlq_total", {"stream": channel})
                return
            except Exception:
                _event_bus_metrics["callbacks_failure"] += 1
                safe_inc("events_delivered_total", {"channel": ch, "status": "failure"})
                if attempt >= self._max_deliveries and not forever:
                    logger.exception(
                        "In-memory delivery dead-lettered after %d attempts "
                        "channel=%s event=%s",
                        attempt,
                        channel,
                        message.get("id"),
                    )
                    self.dead_letters.append((channel, message, attempt))
                    safe_inc("streams_messages_dlq_total", {"stream": channel})
                    return
                logger.warning(
                    "Callback failed for channel=%s (attempt %d/%d); retrying",
                    channel,
                    attempt,
                    self._max_deliveries,
                    exc_info=True,
                )
                delay = min(
                    self._retry_max, self._retry_base * (2 ** min(attempt - 1, 30))
                )
                await asyncio.sleep(delay)
                continue
            _warn_if_callback_slow(channel, time.perf_counter() - started)
            _event_bus_metrics["callbacks_success"] += 1
            safe_inc("events_delivered_total", {"channel": ch, "status": "success"})
            return

    async def drain(self, timeout: float | None = None) -> None:  # noqa: ASYNC109 - explicit per-call timeout is the API
        """Wait until every in-flight delivery (incl. its retries) finished.

        Deliveries may publish further events; loops until quiescent. With a
        ``timeout`` the wait is bounded (``TimeoutError`` if exceeded).
        """

        async def _wait_all() -> None:
            while self._deliveries:
                await asyncio.gather(*tuple(self._deliveries), return_exceptions=True)

        if timeout is None:
            await _wait_all()
        else:
            await asyncio.wait_for(_wait_all(), timeout=timeout)

    async def subscribe(
        self, channel: str, callback: CallbackType
    ) -> SubscriptionHandle:
        validate_channel_name(channel)

        async def _close() -> None:
            async with self._lock:
                callbacks = self._subs.get(channel)
                handles = self._subscription_handles.get(channel)
                if callbacks is None:
                    return
                try:
                    index = callbacks.index(callback)
                except ValueError:
                    return
                callbacks.pop(index)
                if handles is not None and index < len(handles):
                    handles.pop(index)
                if not callbacks:
                    del self._subs[channel]
                    self._subscription_handles.pop(channel, None)

        handle = SubscriptionHandle(_close)
        handle.mark_starting()
        async with self._lock:
            self._subs.setdefault(channel, []).append(callback)
            self._subscription_handles.setdefault(channel, []).append(handle)
        handle.mark_running()
        safe_inc("subscriptions_total", {"channel": channel})
        return handle

    def unsubscribe(self, channel: str, callback: CallbackType) -> None:
        async def _do_remove() -> None:
            async with self._lock:
                if channel in self._subs:
                    try:
                        index = self._subs[channel].index(callback)
                        self._subs[channel].pop(index)
                        handles = self._subscription_handles.get(channel)
                        if handles is not None and index < len(handles):
                            handles.pop(index).mark_stopped()
                        if not self._subs[channel]:
                            del self._subs[channel]
                            self._subscription_handles.pop(channel, None)
                    except ValueError:
                        pass

        # fire and forget — the sync interface is protocol-compatible
        self._track_task(asyncio.create_task(_do_remove()))

    async def close(self) -> None:
        # Drain pending unsubscribe tasks so nothing races the teardown
        await self._drain_bg_tasks(timeout=settings.event_shutdown_drain_seconds)
        # Let in-flight deliveries finish, bounded so a hung handler cannot
        # hang shutdown.
        budget = settings.event_shutdown_drain_seconds
        if self._deliveries:
            _, pending = await asyncio.wait(
                tuple(self._deliveries), timeout=budget if budget > 0 else None
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            for handles in self._subscription_handles.values():
                for handle in handles:
                    handle.mark_stopped()
            self._subs.clear()
            self._subscription_handles.clear()
