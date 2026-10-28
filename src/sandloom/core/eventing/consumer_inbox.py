"""Cache-backed consumer dedupe inbox (best-effort, fail-open)."""

from __future__ import annotations

from collections.abc import Sequence

from sandloom.core.effects import RECORD_TTL_SECONDS
from sandloom.core.infra.access import cache


class ConsumerInbox:
    """Consumer-side dedupe: turns at-least-once delivery into once-per-effect.

    The transport (and the outbox relay) deliver each event AT LEAST once — the
    same stable ``envelope.id`` can arrive more than once. A consumer wraps its
    handler so the side effect runs only the first time an id is seen, keyed per
    logical consumer. Backed by the cache provider's atomic ``add`` (SET NX), so
    it needs no new storage; a longer TTL trades memory for a wider dedupe
    window.

    This inbox is FAIL-OPEN and TTL-bounded: a cache outage degrades to
    at-least-once and a handler whose side effect ran but whose mark was lost
    can re-run. That is correct for IDEMPOTENT / non-critical handlers. For
    money / orders / provisioning — where the effect must run exactly once —
    use :class:`sandloom.core.sql_alchemy.durable_inbox.DurableConsumerInbox`, which
    claims the event in the handler's own DB transaction so the claim and the
    effect commit atomically.
    """

    def __init__(self, consumer: str, *, ttl_seconds: int = RECORD_TTL_SECONDS) -> None:
        self._consumer = consumer
        self._ttl = ttl_seconds

    def _key(self, event_id: str) -> str:
        return f"inbox:{self._consumer}:{event_id}"

    async def seen(self, event_id: str) -> bool:
        """Atomic mark-and-check: True if already seen, else mark and return False.

        Use when the handler is idempotent / marked-before-processing. Fail-open
        (returns False) on a cache error so an outage degrades to at-least-once
        rather than dropping events.
        """
        try:
            stored = await cache().add(self._key(event_id), "1", ttl_seconds=self._ttl)
        except Exception:  # noqa: BLE001 - degrade to at-least-once
            return False
        return not stored

    async def already_processed(self, event_id: str) -> bool:
        """Check (without marking) whether ``event_id`` was processed.

        Paired with :meth:`mark_processed` for the retry pattern: a handler that
        fails must NOT be recorded as processed, so the event is redelivered.
        Fail-open (False) on cache error.
        """
        try:
            return await cache().get(self._key(event_id)) is not None
        except Exception:  # noqa: BLE001 - degrade to at-least-once
            return False

    async def mark_processed(self, event_id: str) -> None:
        """Record ``event_id`` as processed (call only after the handler succeeds)."""
        try:
            await cache().set(self._key(event_id), "1", ttl_seconds=self._ttl)
        except Exception:  # noqa: BLE001, S110 - best effort; redelivery is still safe
            pass

    async def processed_many(self, event_ids: Sequence[str]) -> set[str]:
        """The subset of ``event_ids`` already processed (one ``MGET``).

        Batch form of :meth:`already_processed`; fail-open (empty set) on a
        cache error.
        """
        if not event_ids:
            return set()
        try:
            provider = cache()
            keys = [self._key(event_id) for event_id in event_ids]
            get_many = getattr(provider, "get_many", None)
            if get_many is not None:
                values = await get_many(keys)
            else:  # a cache without the batch op (test doubles)
                values = [await provider.get(key) for key in keys]
        except Exception:  # noqa: BLE001 - degrade to at-least-once
            return set()
        return {
            event_id
            for event_id, value in zip(event_ids, values, strict=True)
            if value is not None
        }

    async def mark_processed_many(self, event_ids: Sequence[str]) -> None:
        """Batch form of :meth:`mark_processed` (one pipelined round trip)."""
        if not event_ids:
            return
        try:
            provider = cache()
            items = {self._key(event_id): "1" for event_id in event_ids}
            set_many = getattr(provider, "set_many", None)
            if set_many is not None:
                await set_many(items, ttl_seconds=self._ttl)
            else:  # a cache without the batch op (test doubles)
                for key, value in items.items():
                    await provider.set(key, value, ttl_seconds=self._ttl)
        except Exception:  # noqa: BLE001, S110 - best effort; redelivery is still safe
            pass
