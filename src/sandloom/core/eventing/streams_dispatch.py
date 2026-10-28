"""Streams bus delivery: run a consumed batch under its lease, mark, ack, DLQ."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import (
    Any,
)

from sandloom.core.config.settings import settings
from sandloom.core.eventing.bus_metrics import _warn_if_callback_slow
from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.eventing.envelope import (
    CallbackType,
    EventEnvelope,
)
from sandloom.core.eventing.streams_inventory import _StreamsInventory
from sandloom.core.eventing.streams_lease import _BatchLease
from sandloom.core.eventing.subscriptions import SubscriptionHandle
from sandloom.core.eventing.transport_base import DLQ_REASON_POISON
from sandloom.core.eventing.transport_capabilities import TransportCapability as Cap
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


async def _inbox_processed_many(inbox: Any, event_ids: list[str]) -> set[str]:
    """Ids already processed (one round trip when the inbox batches)."""
    if not event_ids:
        return set()
    batched = getattr(inbox, "processed_many", None)
    if batched is not None:
        return set(await batched(event_ids))
    return {i for i in event_ids if await inbox.already_processed(i)}


async def _inbox_mark_many(inbox: Any, event_ids: list[str]) -> None:
    batched = getattr(inbox, "mark_processed_many", None)
    if batched is not None:
        await batched(event_ids)
        return
    for event_id in event_ids:
        await inbox.mark_processed(event_id)


class _StreamsDispatch(_StreamsInventory):
    """Batch dispatch: lease renewal, callbacks, inbox marks, acks, poison."""

    _subs: dict[tuple[str, str], list[CallbackType]]
    _subscription_handles: dict[tuple[str, str], list[SubscriptionHandle]]
    _lock: asyncio.Lock

    def _batch_lease(
        self, channel: str, group: str, consumer: str
    ) -> _BatchLease | None:
        """The consume loop's lease keeper (None: the transport has no leases)."""
        if Cap.LEASE_RENEWAL not in self._capabilities:
            return None
        return _BatchLease(
            self._transport.renew_pending,
            self._transport.renew_interval_seconds,
            consumer,
            partial(self._heartbeat, channel, group),
        )

    async def _ack_many(self, deliveries: list[Any]) -> None:
        """Ack ``deliveries`` (one multi-id XACK when the transport batches)."""
        if Cap.BATCH_ACK in self._capabilities:
            await self._transport.ack_many(deliveries)
            return
        for delivery in deliveries:  # third-party transport without ack_many
            await self._transport.ack(delivery)

    async def _dispatch_with_lease(
        self,
        channel: str,
        group: str,
        deliveries: list[Any],
        inbox: Any,
        consumer: str,
        lease: _BatchLease | None = None,
    ) -> None:
        """Run the batch inline while a timer renews its lease.

        Lost lease renewal cancels (rolls back) the active handler rather than
        letting a second replica reclaim it while it applies, and raises the
        renewal's error to the consume loop.
        """
        if lease is None:
            lease = self._batch_lease(channel, group, consumer)
            if lease is None:
                await self._dispatch_batch(channel, group, deliveries, inbox)
                return
        lease.start(deliveries)
        try:
            await self._dispatch_batch(
                channel,
                group,
                deliveries,
                inbox,
                lease_pending=lease.pending,
                lease_lock=lease.lock,
            )
        except asyncio.CancelledError:
            if not lease.cancelled_owner:
                raise
        finally:
            await lease.stop()
        if lease.cancelled_owner:
            task = asyncio.current_task()
            if task is not None and task.uncancel() > 0:
                raise asyncio.CancelledError  # stopped meanwhile as well
            assert lease.error is not None  # noqa: S101 - type narrowing of an invariant
            raise lease.error

    async def _dispatch_batch(
        self,
        channel: str,
        group: str,
        deliveries: list[Any],
        inbox: Any,
        *,
        lease_pending: dict[str, Any] | None = None,
        lease_lock: asyncio.Lock | None = None,
    ) -> None:
        """Run one consumed batch, flushing successes within a bounded interval.

        Redis work is per BATCH, not per event: the inbox dedupe check is one
        ``MGET`` (:meth:`ConsumerInbox.processed_many`; skipped, with the
        marks, when every callback is a durable consumer), the successful
        deliveries' marks one pipeline (:meth:`ConsumerInbox.mark_processed_many`)
        and their acks one multi-id ``XACK`` (``ack_many``). Per-message
        failure handling is unchanged: a delivery whose handler raised is
        neither marked nor acked, so it is redelivered after its backoff
        (at-least-once). Marks and acks of the batch's successes are written
        after at most 50 ms of handler work and again on cancellation. Fast
        batches still use one mark pipeline and one XACK. Exactly-once effects
        continue to require ``durable_inbox``: death between commit and ack
        can always redeliver the final in-flight event.
        """
        ids = [str(d.envelope.get("id", "") or "") for d in deliveries]
        async with self._lock:
            callbacks = list(self._subs.get((channel, group), []))
        inbox = self._batch_inbox(callbacks, inbox)
        processed = (
            await _inbox_processed_many(inbox, [i for i in ids if i])
            if inbox is not None
            else set()
        )
        to_ack: list[Any] = []
        to_mark: list[str] = []
        last_flush = time.monotonic()

        async def flush() -> None:
            nonlocal last_flush
            # Always mark BEFORE ack: a crash in between redelivers a marked id.
            if to_mark:
                await self._mark_processed(inbox, to_mark)
            if to_ack:
                await self._acknowledge(to_ack, lease_pending, lease_lock)
            last_flush = time.monotonic()

        try:
            for delivery, event_id in zip(deliveries, ids, strict=True):
                if event_id and event_id in processed:
                    to_ack.append(delivery)
                else:
                    outcome = await self._run_delivery(
                        channel, group, callbacks, delivery
                    )
                    if outcome == "poison":
                        await self._park_poison(delivery, lease_pending, lease_lock)
                    elif outcome == "ok":
                        if event_id:
                            to_mark.append(event_id)
                            processed.add(event_id)
                        to_ack.append(delivery)
                if time.monotonic() - last_flush >= 0.05:
                    await flush()
        finally:
            await flush()

    @staticmethod
    def _batch_inbox(callbacks: list[CallbackType], inbox: Any) -> Any:
        """The cache inbox a batch dedupes through (``None``: all durable)."""
        # A group whose every callback is durable dedupes in the handler's own
        # UoW (the ``durable_inbox`` claim commits with the effect): the cache
        # inbox would only save opening a UoW on a rare redelivery, at an MGET
        # + a SET pipeline per batch. Any non-durable callback keeps it.
        if callbacks and all(
            getattr(cb, "sandloom_durable", False) for cb in callbacks
        ):
            return None
        return inbox

    @staticmethod
    async def _mark_processed(inbox: Any, to_mark: list[str]) -> None:
        """Record the flushed successes in the cache inbox (if the batch has one)."""
        if inbox is not None:
            await _inbox_mark_many(inbox, to_mark)
        to_mark.clear()

    async def _acknowledge(
        self,
        to_ack: list[Any],
        lease_pending: dict[str, Any] | None,
        lease_lock: asyncio.Lock | None,
    ) -> None:
        """Ack the batch's flushed successes, serialised with the lease renewal."""
        if lease_lock is not None:
            async with lease_lock:
                await self._acknowledge_now(to_ack, lease_pending)
        else:
            await self._acknowledge_now(to_ack, lease_pending)

    async def _acknowledge_now(
        self, to_ack: list[Any], lease_pending: dict[str, Any] | None
    ) -> None:
        """Ack ``to_ack`` and drop it from the lease's pending set."""
        await self._ack_many(to_ack)
        if lease_pending is not None:
            for delivery in to_ack:
                lease_pending.pop(delivery.id, None)
        to_ack.clear()

    async def _run_delivery(
        self,
        channel: str,
        group: str,
        callbacks: list[CallbackType],
        delivery: Any,
    ) -> str:
        """Run every callback on ``delivery`` with the handles' watchdog armed."""
        handles = self._subscription_handles.get((channel, group), [])
        for handle in handles:
            handle.mark_processing(
                settings.event_handler_timeout_seconds * max(1, len(callbacks))
            )
        try:
            return await self._run_callbacks_outcome(
                channel, callbacks, delivery.envelope
            )
        finally:
            for handle in handles:
                handle.mark_progress()

    async def _park_poison(
        self,
        delivery: Any,
        lease_pending: dict[str, Any] | None,
        lease_lock: asyncio.Lock | None,
    ) -> None:
        """Dead-letter a poison delivery, serialised with the lease renewal."""
        if lease_lock is not None:
            async with lease_lock:
                await self._dead_letter_poison(delivery)
                if lease_pending is not None:
                    lease_pending.pop(delivery.id, None)
        else:
            await self._dead_letter_poison(delivery)

    async def _dispatch(
        self, channel: str, group: str, delivery: Any, inbox: Any
    ) -> None:
        """Dispatch ONE delivery (a batch of one; kept for callers/tests)."""
        await self._dispatch_batch(channel, group, [delivery], inbox)

    @classmethod
    async def _run_callbacks(
        cls, channel: str, callbacks: list[CallbackType], envelope: EventEnvelope
    ) -> bool:
        """Run every callback; True only if all of them succeeded."""
        return await cls._run_callbacks_outcome(channel, callbacks, envelope) == "ok"

    @staticmethod
    async def _run_callbacks_outcome(
        channel: str, callbacks: list[CallbackType], envelope: EventEnvelope
    ) -> str:
        """Run every callback: ``"ok"``, ``"failed"`` (redeliver) or ``"poison"``
        (a callback raised :class:`PoisonEventError`: it can never apply)."""

        outcome = "ok"
        for cb in callbacks:
            try:
                started = time.perf_counter()
                async with asyncio.timeout(settings.event_handler_timeout_seconds):
                    await cb(envelope)
                _warn_if_callback_slow(channel, time.perf_counter() - started)
                safe_inc(
                    "events_delivered_total", {"channel": channel, "status": "success"}
                )
            except PoisonEventError:
                logger.critical(
                    "Streams callback rejected a poison event channel=%s event=%s",
                    channel,
                    envelope.get("id"),
                )
                outcome = "poison"
                safe_inc(
                    "events_delivered_total", {"channel": channel, "status": "failure"}
                )
            except Exception:
                logger.exception("Streams callback failed channel=%s", channel)
                if outcome == "ok":
                    outcome = "failed"
                safe_inc(
                    "events_delivered_total", {"channel": channel, "status": "failure"}
                )
        return outcome

    async def _dead_letter_poison(self, delivery: Any) -> None:
        """Park a poison delivery in the DLQ now (left pending if unsupported)."""
        try:
            await self._transport.dead_letter(delivery, reason=DLQ_REASON_POISON)
        except NotImplementedError:
            logger.critical(
                "Transport %s cannot dead-letter; poison event left pending "
                "channel=%s id=%s",
                type(self._transport).__name__,
                delivery.stream,
                delivery.id,
            )
