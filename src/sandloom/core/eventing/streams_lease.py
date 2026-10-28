"""Lease keeper of a Streams consume loop's in-flight batch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import (
    Any,
)


class _BatchLease:
    """Keeps a consume loop's in-flight batch leased while it is dispatched.

    One per consume loop, reused for every batch: the batch runs INLINE in
    the loop's task and a ``call_later`` timer renews the lease every
    ``interval`` (a renewal task exists only while a renewal is due, i.e.
    for batches running longer than the interval). A failed renewal (lease
    lost to a successor, or Redis unreachable) records the error and cancels
    the loop's task, so the active handler is cancelled and its UoW rolled
    back; :meth:`StreamsEventBus._dispatch_with_lease` turns that
    cancellation into the error. ``lock`` serialises a renewal with the
    batch's acks / dead-letters, so a renewal never counts an entry the batch
    just acked as lost.
    """

    __slots__ = (
        "_consumer",
        "_heartbeat",
        "_interval",
        "_owner",
        "_renew",
        "_renewal",
        "_timer",
        "active",
        "cancelled_owner",
        "error",
        "lock",
        "pending",
    )

    def __init__(
        self,
        renew: Callable[[list[Any], str], Awaitable[None]],
        interval: float,
        consumer: str,
        heartbeat: Callable[[], Awaitable[None]],
    ) -> None:
        self._renew = renew
        self._interval = interval
        self._consumer = consumer
        self._heartbeat = heartbeat
        self._owner: asyncio.Task[Any] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._renewal: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.pending: dict[str, Any] = {}
        self.active = False
        self.cancelled_owner = False
        self.error: BaseException | None = None

    def start(self, deliveries: list[Any]) -> None:
        self.pending = {delivery.id: delivery for delivery in deliveries}
        self.error = None
        self.cancelled_owner = False
        self.active = True
        self._owner = asyncio.current_task()
        self._timer = asyncio.get_running_loop().call_later(self._interval, self._due)

    def _due(self) -> None:
        self._timer = None
        if self.active:
            self._renewal = asyncio.get_running_loop().create_task(self._run_renewal())

    async def _run_renewal(self) -> None:
        try:
            async with self.lock:
                await self._renew(list(self.pending.values()), self._consumer)
            await self._heartbeat()
        except Exception as exc:  # noqa: BLE001 - surfaced through the owner
            if self.active and self._owner is not None:
                self.error = exc
                self.cancelled_owner = True
                self._owner.cancel()
            return
        if self.active:
            self._timer = asyncio.get_running_loop().call_later(
                self._interval, self._due
            )

    async def stop(self) -> None:
        """End the batch's lease: no renewal runs or starts after this."""
        self.active = False  # before any await: a late failure cancels nobody
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        renewal, self._renewal = self._renewal, None
        if renewal is not None and not renewal.done():
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
