"""Relational (DB-backed) durable consumer inbox — exactly-once *effect*.

This is the relational implementation behind ``sandloom.core.di.durable_inbox``: an
adapter of the durable dedupe record (ADR-0005,
:mod:`sandloom.core.sql_alchemy.dedupe_log`) onto the ``consumer_inbox`` table —
scope = consumer, key = event id, no TTL (purged by age), nothing to replay.
It lives in the relational paradigm subtree (not in the paradigm-agnostic
eventing transport) because it is intrinsically relational: it claims an event
by inserting a ``UNIQUE(consumer, event_id)`` row in the handler's Unit of
Work, so the claim and the side effect commit atomically.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.effects import PROCESSED, DedupeRecordSpec
from sandloom.core.sql_alchemy.dedupe_log import RelationalDedupeLog
from sandloom.core.sql_alchemy.models.consumer_inbox import (
    ConsumerInboxEntry,
    _utc_naive_now,
)

INBOX_RECORD = DedupeRecordSpec(
    table="consumer_inbox",
    conflict=("consumer", "event_id"),
    columns=("id", "consumer", "event_id", "status", "processed_at"),
    returning="id",
    purge_column="processed_at",
    replay_status=PROCESSED,
    constraint="uq_consumer_inbox_consumer_event",
)

#: The inbox's durable dedupe log (one per process; stateless but for its
#: prebuilt statements).
INBOX_LOG = RelationalDedupeLog(
    INBOX_RECORD, lambda: cast(Table, ConsumerInboxEntry.__table__)
)
#: dialect -> the prebuilt claim INSERT (every value a bind parameter): built
#: once instead of constructing + cache-keying a statement per delivery.
_claim_statements: dict[str, Any] = INBOX_LOG.claim_statements


class DurableConsumerInbox:
    """DB-backed consumer dedupe: exactly-once *effect* for critical handlers.

    Unlike the cache-backed ``ConsumerInbox`` (fail-open, TTL-bounded), this
    claims an event by inserting a ``UNIQUE(consumer, event_id)`` row in the
    HANDLER'S Unit of Work, so the claim and the side effect commit atomically.
    A duplicate delivery hits the unique constraint and is skipped; there is no
    window where the effect ran but the claim was lost.

    Usage inside a handler that already owns a UoW session::

        inbox = DurableConsumerInbox("billing.credits@orders.paid.v1")
        if not await inbox.claim(session, envelope["id"]):
            return  # duplicate delivery — effect already applied
        ...apply the side effect on the same session...
    """

    def __init__(self, consumer: str) -> None:
        self._consumer = consumer

    async def claim(self, session: AsyncSession, event_id: str) -> bool:
        """Claim ``event_id`` for this consumer within ``session``'s transaction.

        Returns ``True`` if newly claimed (run the side effect), ``False`` if a
        prior delivery already claimed it (skip the side effect).

        The claim is a single Core ``INSERT ... ON CONFLICT (consumer, event_id)
        DO NOTHING RETURNING id`` executed with autoflush DISABLED: only "this
        exact claim already exists" reads as a duplicate. The previous
        ``session.add`` + ``flush()`` flushed EVERY pending object of the
        handler's session inside the savepoint, so the handler's OWN constraint
        violation was misread as "duplicate → skip" and the effect was silently
        lost. Any other database error now propagates (the UoW rolls back and
        the event is redelivered).
        """
        claim = await INBOX_LOG.claim(
            session,
            {
                "id": uuid.uuid4(),
                "consumer": self._consumer,
                "event_id": event_id,
                "status": PROCESSED,
                "processed_at": _utc_naive_now(),
            },
        )
        return claim.outcome == "claimed"

    @staticmethod
    async def purge_processed_before(
        session: AsyncSession, before: datetime, *, limit: int | None = None
    ) -> int:
        """Delete inbox claims older than ``before``; return rows removed.

        The ``consumer_inbox`` table grows one row per processed event and has
        no TTL (unlike the cache-backed inbox). Retention is an operator
        responsibility: schedule this with a cutoff comfortably past the
        outbox/transport redelivery window so a late duplicate still finds its
        claim. Runs in the caller's transaction.

        ``limit`` bounds one call to that many rows (``id IN (SELECT … LIMIT
        n)``, served by ``ix_consumer_inbox_processed_at``) so a caller can
        purge in short transactions; ``None`` deletes every match at once.
        """
        return await INBOX_LOG.purge(session, before, limit=limit)


def durable_inbox(consumer: str) -> Any:
    """Return a :class:`DurableConsumerInbox` for exactly-once consumer effects.

    For money / orders / provisioning handlers: claim the event in the
    handler's own UoW session before applying the side effect::

        inbox = durable_inbox("billing.credits@orders.paid.v1")
        if not await inbox.claim(session, envelope["id"]):
            return  # duplicate delivery — effect already applied

    Non-critical / idempotent handlers can keep using the cache-backed
    at-least-once dedupe instead.
    """
    return DurableConsumerInbox(consumer)


__all__ = ["durable_inbox", "INBOX_LOG", "INBOX_RECORD", "DurableConsumerInbox"]
