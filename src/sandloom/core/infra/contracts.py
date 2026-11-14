"""Abstract infrastructure provider contracts (technology-agnostic).

The kernel and business modules interact with infrastructure ONLY through
these abstractions. A concrete backend (Postgres, Redis, S3, Celery, …) is a
*driver* that subclasses one of these capability base classes and is selected
purely by configuration — modules never import a concrete driver.

Capabilities are intentionally small and orthogonal:

* ``database``    — transactional persistence (exposes a UnitOfWork factory)
* ``cache``       — ephemeral key/value store
* ``objectstore`` — blob/file storage
* ``taskqueue``   — background job broker
* ``eventbus``    — pub/sub domain-event transport

Storage is split *by paradigm*, not by product. ``database`` is the
**relational paradigm** contract — the rich, transactional system-of-record
that backs the Unit of Work, the transactional outbox and the audit log; its
guarantees (atomic UoW, ``transactional_outbox``, ``audit``) are advertised as
capability *features* a driver must support. Other storage paradigms have their
OWN contracts with their own (often weaker) guarantees: ``document_store`` and
``graph_store`` (see :mod:`sandloom.core.infra.extra_capabilities`). A module picks
the paradigm whose contract fits, rather than forcing one universal "database"
abstraction to span them all — that is why the relational contract is allowed
to be SQL-shaped while a document/graph contract is not.

Every capability supports *named instances*: multiple databases (or caches,
or queues) can coexist in one process, each addressed by a logical name the
module knows nothing concrete about (``"default"``, ``"analytics"``, …).
"""

from __future__ import annotations

import abc
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.ports.cache import (
    SET_MAX_MAX_ROUNDS,
    SUBJECT_STATE_RANKS,
    Cache,
    CacheContentionError,
    CacheProvider,
    parse_plain_decimal,
    parse_subject_state,
    portable_set_max,
    portable_set_subject_state,
)
from sandloom.core.infra.ports.database import DatabaseProvider, UnitSession
from sandloom.core.infra.ports.eventbus import EventBusProvider
from sandloom.core.infra.ports.notification import (
    Notification,
    NotificationChannel,
    Notifier,
    NotifierProvider,
)
from sandloom.core.infra.ports.objectstore import (
    ObjectContent,
    ObjectReader,
    ObjectStore,
    ObjectStoreProvider,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.core.infra.ports.taskqueue import (
    PeriodicSpec,
    TaskQueue,
    TaskQueueProvider,
    TaskSpec,
    TaskTable,
)


@dataclass(frozen=True)
class OutboxHandle:
    """A backend-agnostic reference to an enqueued outbox event.

    ``id`` is the stable envelope id (also the event id used for dedupe);
    ``raw`` is an opaque backend handle (e.g. the ORM row) the store uses to
    tell a committed row from one undone by a rolled-back SAVEPOINT.
    """

    id: uuid.UUID
    raw: object | None = None
    trace_id: str | None = None
    emitted_at: datetime | None = None


class BrokerUnavailableError(Exception):
    """The event transport is unreachable (connection refused, timeout, …).

    Distinct from a *poison* message: a broker outage says nothing about the
    event itself, so the outbox relay must NOT burn the event's delivery budget
    on it (a 20s Redis blip would otherwise dead-letter every pending row). The
    relay wraps transport/connection errors in this type; an
    :class:`OutboxStore` records such a failure without counting an attempt and
    the relay stops the batch (circuit-breaker) until the broker recovers.
    """


#: The relay's batch publisher: ``[(channel, payload, schema_version,
#: event_id), ...]`` published IN ORDER in one transport round trip. Raises
#: :class:`BrokerUnavailableError` on an outage (nothing counted, nothing
#: marked) or any other exception when the batch failed for a reason that
#: must be attributed to ONE row (the store then retries that batch row by
#: row with the per-event ``publish``).
PublishMany = Callable[
    [Sequence[tuple[str, dict[str, Any], str, str]]], Awaitable[None]
]


#: Outcome of one :meth:`OutboxStore.deliver_pending` call.
DeliveryStatus = Literal["delivered", "failed", "dead", "skipped", "unavailable"]


@dataclass(frozen=True)
class RelayBatchResult:
    """Outcome of one :meth:`OutboxStore.deliver_batch` call.

    ``claimed`` rows were taken for this batch; each ended ``delivered``,
    ``failed`` (attempt counted, backed off; ``dead`` of them exhausted the
    budget) or ``skipped`` (settled/reclaimed elsewhere, released
    unpublished, or held behind an earlier failed row of its channel).
    ``unavailable`` means the broker was unreachable: the batch stopped
    without counting attempts. ``full`` (as many rows claimed as the
    limit) tells the relay more may be due right away.
    """

    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    skipped: int = 0
    unavailable: bool = False
    full: bool = False
    dead_ids: tuple[uuid.UUID, ...] = ()

    @property
    def attempted(self) -> int:
        """Rows whose publish outcome was recorded (delivered + failed)."""
        return self.delivered + self.failed


class OutboxStore(abc.ABC):
    """Transactional outbox writer for one database backend.

    Encapsulates how a backend persists and marks events so the public
    ``publish_event`` adapter never imports a concrete ORM model. Only backends
    that advertise the ``transactional_outbox`` feature provide one.
    """

    @abc.abstractmethod
    def enqueue(
        self,
        session: UnitSession,
        channel: str,
        payload: Mapping[str, Any],
        schema_version: str,
    ) -> OutboxHandle:
        """Write an event into the outbox within the current transaction."""

    def survived(self, handle: OutboxHandle) -> bool:
        """True if the enqueued row survived (not undone by a SAVEPOINT)."""
        return True

    @abc.abstractmethod
    async def mark_delivered(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        ids: Sequence[uuid.UUID],
    ) -> None:
        """Mark the given events delivered in their own database."""

    # -- relay side: the outbox relay orchestrates through these, so it never
    #    knows the backend's storage (SQL row locking, a Mongo changelog, …).
    @abc.abstractmethod
    async def fetch_pending_ids(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        max_attempts: int,
        grace_seconds: float,
        limit: int,
    ) -> Sequence[uuid.UUID]:
        """Return ids of undelivered, non-dead events older than the grace."""

    @abc.abstractmethod
    async def deliver_pending(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        message_id: uuid.UUID,
        publish: Callable[[str, dict[str, Any], str, str], Awaitable[None]],
        *,
        max_attempts: int,
        backoff: Callable[[int], float] | None = None,
    ) -> DeliveryStatus:
        """Deliver one event in its own transaction (claim, publish, mark).

        ``publish(channel, payload, schema_version, event_id)`` is supplied by
        the relay (already wrapped with its transport timeout). The store claims
        the row (so concurrent relays don't double-process), calls ``publish``,
        and records the outcome: ``delivered``, ``failed`` (attempt incremented,
        next attempt deferred by ``backoff(attempts)`` seconds), ``dead``
        (attempts exhausted), ``unavailable`` (``publish`` raised
        :class:`BrokerUnavailableError` — NO attempt is counted; the row is
        released for a later retry), or ``skipped`` (already handled).
        """

    async def deliver_batch(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        publish: Callable[[str, dict[str, Any], str, str], Awaitable[None]],
        *,
        max_attempts: int,
        grace_seconds: float,
        limit: int,
        backoff: Callable[[int], float] | None = None,
        publish_many: PublishMany | None = None,
    ) -> RelayBatchResult:
        """Claim up to ``limit`` due events, publish them in order, settle all.

        The relay's unit of work. Same semantics as :meth:`fetch_pending_ids`
        + one :meth:`deliver_pending` per id (exactly what this default does);
        a backend overrides it to claim and settle the whole batch in a
        constant number of round trips. A publish raising
        :class:`BrokerUnavailableError` stops the batch: that row is parked
        without counting an attempt, the unpublished rest is left untouched
        (``unavailable=True``). ``publish_many`` (the relay's one-round-trip
        batch publisher) MAY be used by a backend that claims the whole batch
        up front; this per-row default ignores it.
        """
        ids = list(
            await self.fetch_pending_ids(
                uow_factory,
                max_attempts=max_attempts,
                grace_seconds=grace_seconds,
                limit=limit,
            )
        )
        delivered = failed = dead = skipped = 0
        dead_ids: list[uuid.UUID] = []
        unavailable = False
        for message_id in ids:
            status = await self.deliver_pending(
                uow_factory,
                message_id,
                publish,
                max_attempts=max_attempts,
                backoff=backoff,
            )
            if status == "unavailable":
                unavailable = True
                break
            if status == "skipped":
                skipped += 1
            elif status == "delivered":
                delivered += 1
            elif status == "dead":
                dead += 1
                failed += 1
                dead_ids.append(message_id)
            else:
                failed += 1
        return RelayBatchResult(
            claimed=len(ids),
            delivered=delivered,
            failed=failed,
            dead=dead,
            skipped=skipped,
            unavailable=unavailable,
            full=len(ids) >= limit,
            dead_ids=tuple(dead_ids),
        )

    async def purge_delivered(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        older_than_seconds: float,
        limit: int,
    ) -> int:
        """Delete up to ``limit`` events delivered more than ``older_than`` ago.

        Outbox retention (``OUTBOX_RETENTION_HOURS``): a delivered row is
        history only — the relay never reads it again. Dead-lettered and
        pending rows are never purged. Returns the rows deleted (default: the
        backend keeps no such history, 0).
        """
        return 0

    async def oldest_pending_age_seconds(
        self, uow_factory: Callable[[], UnitOfWorkContract]
    ) -> float | None:
        """Age of the oldest undelivered, non-dead row (``0.0`` = none pending).

        Feeds ``sandloom_outbox_oldest_pending_age_seconds`` (relay). Must be cheap
        (an index lookup, never a scan). Default ``None``: not measurable.
        """
        return None

    async def dead_letter_exhausted(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        max_attempts: int,
    ) -> int:
        """Dead-letter rows whose attempts already reach ``max_attempts``.

        Rows can exceed the budget without having been marked dead when the
        operator LOWERS the max-attempts setting; without this sweep they would
        never be retried nor surfaced. Returns the number of rows moved to the
        dead-letter state. Default: nothing to do (backends without the notion).
        """
        return 0

    async def list_dead(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        channel: str | None = None,
        limit: int = 100,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Return dead-lettered events (id, channel, attempts, error, dead_at)."""
        raise NotImplementedError(f"{type(self).__name__} cannot list dead events")

    async def requeue_dead(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        ids: Sequence[uuid.UUID] | None = None,
        channel: str | None = None,
        limit: int = 1000,
    ) -> int:
        """Return dead-lettered events to the pending state (operator repair).

        Clears the dead-letter marker and resets the attempt budget so the relay
        redelivers them (same stable event id — consumers still dedupe). Scoped
        by explicit ``ids`` and/or ``channel``; returns rows requeued.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot requeue dead events")

    async def list_channel_since(
        self,
        session: UnitSession,
        *,
        channel: str,
        since: datetime,
        limit: int = 1000,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Keyset page of outbox rows of ``channel`` emitted at/after ``since``.

        Oldest first; ``after`` is the ``(emitted_at, id)`` of the last row
        already seen. Rows carry ``id``, ``payload`` and ``emitted_at`` (naive
        UTC), delivered or not — the security-state reconciliation re-derives
        revocation marks from them after a cache restore
        (:mod:`sandloom.core.auth.recovery`). Rows already purged past
        ``OUTBOX_RETENTION_HOURS`` are gone.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot list events")


class AuditStore(abc.ABC):
    """Audit-log writer with an optional bounded operator query capability."""

    async def query(
        self,
        session: UnitSession,
        *,
        skip: int,
        limit: int,
        action: str | None,
        actor_id: uuid.UUID | None,
        resource_id: str | None,
    ) -> list[dict[str, Any]]:
        """Read sanitized audit metadata. Unsupported drivers fail explicitly."""
        raise NotImplementedError(
            "Audit queries are unsupported by this database driver"
        )

    async def query_page(
        self,
        session: UnitSession,
        *,
        limit: int,
        before: tuple[datetime, uuid.UUID] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        action: str | None = None,
        action_prefix: str | None = None,
        actor_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Keyset page of sanitized audit metadata, newest first.

        ``before`` is the ``(occurred_at, id)`` of the last row already seen;
        rows strictly after it in ``occurred_at DESC, id DESC`` order are
        returned. No COUNT is computed. ``since`` is inclusive, ``until``
        exclusive; ``action_prefix`` is a literal prefix (no wildcards).
        """
        raise NotImplementedError(
            "Audit queries are unsupported by this database driver"
        )

    @abc.abstractmethod
    def record(
        self,
        session: UnitSession,
        *,
        action: str,
        actor_id: uuid.UUID | None,
        actor_type: str,
        resource_type: str | None,
        resource_id: str | None,
        data: dict[str, Any] | None,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        """Append an audit entry within the current transaction."""


class CommandLogBusy(Exception):  # a state, not an error kind
    """A concurrent call holds the same command key past the lock timeout."""


@dataclass(frozen=True, slots=True)
class CommandClaim:
    """Outcome of :meth:`CommandLogStore.claim`.

    ``outcome`` is ``"claimed"`` (run the command; the row commits with it),
    ``"replay"`` (a completed call with the same arguments: ``status`` /
    ``result`` are its stored record) or ``"mismatch"`` (the key was used with
    different arguments).
    """

    outcome: Literal["claimed", "replay", "mismatch"]
    status: str | None = None
    result: str | None = None
    #: the replayed call's stored call effects (JSON), re-applied to the caller
    effects: str | None = None


class CommandLogStore(abc.ABC):
    """Durable command-contract idempotency log for one database backend.

    The source of truth for "this command already ran": a row keyed by
    ``(contract, method, bound tenant, idempotency key)`` is claimed IN the
    command's own Unit of Work before its body runs and completed with the
    encoded result before the commit — so the record and the business change
    commit (or roll back) together. Backends advertising the
    ``command_idempotency`` feature provide one.
    """

    @abc.abstractmethod
    async def claim(
        self,
        session: UnitSession,
        *,
        dedupe_key: str,
        contract: str,
        tenant: str | None,
        args_digest: str,
        ttl_seconds: int,
    ) -> CommandClaim:
        """Claim ``dedupe_key`` in ``session``'s transaction (see the class doc).

        A concurrent in-flight claim of the same key blocks until that
        transaction ends (then replays / runs afresh); raises
        :class:`CommandLogBusy` when the wait exceeds the lock timeout. An
        expired record is taken over (the key runs afresh).
        """

    @abc.abstractmethod
    async def complete(
        self,
        session: UnitSession,
        *,
        dedupe_key: str,
        status: str,
        result: str | None,
        effects: str | None = None,
    ) -> None:
        """Store the call's outcome on the claimed row (same transaction):
        its result and the call effects it left on its caller (JSON or
        ``None``), both returned by a replaying :meth:`claim`."""

    @abc.abstractmethod
    async def amend_effects(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        dedupe_key: str,
        effects: str | None,
    ) -> None:
        """Replace the call effects of the COMMITTED record ``dedupe_key`` in
        a unit of its own: what the call's ``on_commit`` callbacks left on
        its caller is known only after they ran, after the commit."""

    async def purge_expired(
        self, uow_factory: Callable[[], UnitOfWorkContract], *, limit: int
    ) -> int:
        """Delete up to ``limit`` expired records; return rows deleted."""
        return 0


class RequestLogBusy(Exception):  # a state, not an error kind
    """Another in-flight request holds the same HTTP idempotency key."""


class RequestLogStore(abc.ABC):
    """Durable HTTP idempotency log (``idempotency_guard(durable=True)``).

    The third adapter of the durable dedupe record (ADR-0005): a row keyed by
    ``(route scope, caller, tenant, Idempotency-Key)`` is claimed IN the
    request's own Unit of Work before the endpoint runs and completed with
    the response right before ``COMMIT`` — so the record and the endpoint's
    effect commit (or roll back) together. Backends advertising the
    ``http_idempotency`` feature provide one. Claims return a
    :class:`CommandClaim` (same three outcomes).
    """

    @abc.abstractmethod
    async def claim(
        self,
        session: UnitSession,
        *,
        dedupe_key: str,
        scope: str,
        tenant: str | None,
        fingerprint: str,
        ttl_seconds: int,
    ) -> CommandClaim:
        """Claim ``dedupe_key`` in ``session``'s transaction.

        Never waits for a concurrent request holding the same key: raises
        :class:`RequestLogBusy` at once. An expired record is taken over.
        """

    @abc.abstractmethod
    async def complete(
        self, session: UnitSession, *, dedupe_key: str, status: str, result: str | None
    ) -> None:
        """Store the response on the claimed row (same transaction)."""

    @abc.abstractmethod
    async def release(self, session: UnitSession, *, dedupe_key: str) -> None:
        """Drop the claimed row (same transaction): the request's outcome is
        not to be replayed (a ``5xx`` the endpoint returned)."""

    async def purge_expired(
        self, uow_factory: Callable[[], UnitOfWorkContract], *, limit: int
    ) -> int:
        """Delete up to ``limit`` expired records; return rows deleted."""
        return 0


#: Readability alias: the ``database`` capability IS the relational paradigm.
#: Use it where calling the contract out as "relational" aids clarity; the
#: canonical capability key stays ``"database"`` (there is no second name).
RelationalProvider = DatabaseProvider


__all__ = [
    "Cache",
    "InfraProvider",
    "Notifier",
    "ObjectStore",
    "TaskQueue",
    "DatabaseProvider",
    "UnitSession",
    "RelationalProvider",
    "CacheProvider",
    "CacheContentionError",
    "SET_MAX_MAX_ROUNDS",
    "parse_plain_decimal",
    "portable_set_max",
    "portable_set_subject_state",
    "parse_subject_state",
    "SUBJECT_STATE_RANKS",
    "ObjectContent",
    "ObjectReader",
    "ObjectStoreProvider",
    "iter_object_chunks",
    "validate_object_key",
    "TaskQueueProvider",
    "TaskSpec",
    "TaskTable",
    "PeriodicSpec",
    "EventBusProvider",
    "NotifierProvider",
    "Notification",
    "NotificationChannel",
    "BrokerUnavailableError",
    "DeliveryStatus",
    "OutboxHandle",
    "OutboxStore",
    "PublishMany",
    "RelayBatchResult",
    "AuditStore",
    "CommandClaim",
    "CommandLogBusy",
    "CommandLogStore",
    "RequestLogBusy",
    "RequestLogStore",
]
