"""The ``database`` capability's spec (the relational paradigm; spec section 11).

Its features and their kinds live here, beside the other capabilities' specs,
with the provider ABC, :class:`UnitSession` (the kernel's view of a unit's
session that the stores and :meth:`DatabaseProvider.has_pending_changes` take),
:class:`ExecutionAdapter` (what :meth:`DatabaseProvider.planner_adapter`
returns) and the stores the ABC returns: outbox, audit, command log and request
log. :mod:`sandloom.core.infra.contracts` re-exports them (kernel-internal).

The driver door may name SQLAlchemy types (the kernel's relational surface
already does): the ones below are imported for type checking only.
"""

from __future__ import annotations

import abc
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, FeatureKind, InfraProvider

if TYPE_CHECKING:
    from sqlalchemy import MetaData
    from sqlalchemy.ext.asyncio import AsyncSession

    from sandloom.core.contracts.uow import UnitOfWorkContract
    from sandloom.core.planner import (
        ExecutionContext,
        Plan,
        ReadOperation,
        ReadUnit,
    )


class UnitSession(Protocol):
    """A unit's session, as the kernel sees it.

    The object a unit of work (:class:`~sandloom.core.contracts.uow.UnitOfWorkContract`)
    exposes as ``session`` and the stores of :class:`DatabaseProvider` write
    through. The kernel only hands it from the unit to a store of the same
    database, and asks the database whether it holds unflushed changes
    (:meth:`DatabaseProvider.has_pending_changes`); the driver knows the
    concrete type (an SQLAlchemy ``AsyncSession``, say) and a store narrows it.
    """

    async def flush(self) -> None:
        """Write the session's pending changes inside its transaction."""

    def in_transaction(self) -> bool:
        """Whether the session has begun a transaction."""


class DatabaseProvider(InfraProvider):
    """Transactional persistence capability — the **relational paradigm** contract.

    This is the framework's privileged system-of-record: it backs the Unit of
    Work, the transactional outbox and the audit log. The only thing the kernel
    needs from a driver is a factory that produces
    :class:`~sandloom.core.contracts.uow.UnitOfWorkContract` instances (plus the
    optional :meth:`outbox`/:meth:`audit` writers gated by the
    ``transactional_outbox`` / ``audit`` features). Everything else (engine,
    pool, dialect, ORM) is the driver's private business — which is why a driver
    may be any SQLAlchemy-supported engine, yet the kernel imports no SQLAlchemy.

    Non-relational stores are NOT modelled here: they have their own paradigm
    contracts (``DocumentStoreProvider``, ``GraphStoreProvider``, …) with their
    own guarantees. :data:`RelationalProvider` is a readability alias for this
    class.
    """

    capability: ClassVar[str] = "database"

    @abc.abstractmethod
    def uow_factory(self) -> Callable[[], UnitOfWorkContract]:
        """Return a zero-arg factory producing UnitOfWork instances."""

    @property
    def session_factory(self) -> Callable[[], AsyncSession] | None:
        """This instance's raw-session factory: calling it yields a session
        of THIS instance (the configured one, whatever URL the infrastructure
        gave it), to be used as an async context manager. ``None`` (the
        default): the driver has no raw sessions (the unit of work is its
        only door). The request dependency ``get_db`` takes the ``default``
        instance's."""
        return None

    def planner_adapter(self, *, contract: bool = False) -> ExecutionAdapter | None:
        """The adapter that runs the planner's declared operations
        (:mod:`sandloom.core.planner`) on this database; ``contract=True``:
        the one on the dedicated contract pool, when the driver keeps one
        (else the regular adapter). ``None`` (the default): the database
        cannot run declared operations, and the planner refuses with a
        diagnostic."""
        return None

    def outbox(self) -> OutboxStore | None:
        """Return this database's transactional outbox writer.

        ``None`` when the backend has no transactional outbox (i.e. does not
        advertise the ``transactional_outbox`` feature). ``publish_event``
        refuses to commit an event to a database that cannot store it durably.
        """
        return None

    def audit(self) -> AuditStore | None:
        """Return this database's audit-log writer (``None`` if unsupported)."""
        return None

    def command_log(self) -> CommandLogStore | None:
        """Return this database's durable command-idempotency log.

        ``None`` when the backend cannot record command outcomes atomically
        with the command (no ``command_idempotency`` feature): command
        contracts on it fall back to the cache-only dedupe.
        """
        return None

    def has_pending_changes(self, session: UnitSession) -> bool:
        """Whether ``session`` (a unit of this database) holds changes that
        its COMMIT would write although no write statement ran yet (an ORM
        identity map with unflushed changes). The commit hooks count them as
        a write (the commit-time re-check, risk triggers). A driver without
        deferred writes answers ``False`` (the default)."""
        return False

    def request_log(self) -> RequestLogStore | None:
        """Return this database's durable HTTP idempotency log.

        ``None`` when the backend cannot record a response atomically with
        the request's transaction (no ``http_idempotency`` feature): a
        ``durable=True`` idempotency guard on it refuses with ``503``.
        """
        return None

    async def ensure_model_schema(
        self, metadata: MetaData | None = None, *, recreate: bool
    ) -> bool:
        """Build the schema of ``metadata`` (the mapped tables; ``None``: every
        table the driver's models map) when this
        database takes its schema from the models instead of the migration
        chains; refuse one whose tables differ from the models, or recreate
        it with ``recreate`` (SANDLOOM-B028). Called once at startup. False
        (the default): the migration chains own this database's schema."""
        return False


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
    on it (a 20s broker blip would otherwise dead-letter every pending row). The
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


class ExecutionAdapter(Protocol):
    """What a database driver offers the planner (one per database instance)."""

    #: Prepared plans, keyed by (id(operation), optimized mode).
    plans: dict[tuple[int, bool], Plan[Any]]

    def compile(self, op: ReadOperation[Any], *, optimized: bool) -> Plan[Any]:
        """Validate ``op`` against the schema and return its plan."""

    async def fetch_one(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any]
    ) -> Sequence[Any] | None:
        """Run ``plan`` under ``ctx`` in its own read-only transaction."""

    async def fetch_in_unit(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any], unit: Any
    ) -> Sequence[Any] | None:
        """Run ``plan``'s GENERAL form on ``unit`` (an open Unit of Work)."""

    def read_unit(self, *, snapshot: bool = False) -> ReadUnit:
        """A scope-wide read-only unit (one lease + transaction, opened lazily);
        ``snapshot=True``: one snapshot for all its reads, refused with
        :class:`~sandloom.core.planner.SnapshotReadsUnavailable` before any I/O
        where the database has no ``snapshot_reads``."""

    async def fetch_page(
        self,
        plan: Plan[Any],
        ctx: ExecutionContext,
        key: Sequence[Any],
        limit: int,
        after: Sequence[Any] | None,
        unit: Any = None,
    ) -> list[Sequence[Any]]:
        """A ListOperation page (``limit`` rows after ``after``): own read-only
        transaction, or the GENERAL form on ``unit`` when given."""


# The relational paradigm. ``features`` are its guarantee-set: a driver that
# cannot do ``transactional_outbox`` simply does not advertise it, and a module
# that publishes events requires it — so the guarantee is capability-gated, not
# assumed. Sibling storage paradigms (document_store, graph_store, …) are
# separate specs in ``extra_capabilities`` with their own, weaker features.

#: The ``database`` features a SQL dialect renderer may declare for its
#: database (design/infra-agnostic/spec.md, section 2): an instance advertises
#: them only when its URL's renderer does.
DATABASE_RENDERER_FEATURES = frozenset(
    {
        # Write transactions overlap, each holding its own row locks (what a
        # race test needs). Never declared by a renderer: derived from its
        # ``serial_writers`` fact (``portable.renderer_features``), so the
        # two cannot disagree.
        "concurrent_writers",
        "skip_locked",
        "transaction_locks",
        "snapshot_reads",
        "readonly_transactions",
        "arrays",
        "fulltext_search",
        "json_path",
        "json_contains",
        "list_aggregates",
    }
)

#: The kind of each ``database`` feature (spec 11): the unit's call shapes are
#: behaviours, what the kernel's guarantees rest on are guarantees, and the
#: renderer features are behaviours of the SQL the renderer emits.
_DATABASE_FEATURE_KINDS: dict[str, FeatureKind] = {
    "transactions": "behaviour",
    "savepoints": "behaviour",
    "write_admission": "guarantee",
    "transactional_outbox": "guarantee",
    "audit": "guarantee",
    "tenant_rls": "guarantee",
    "command_idempotency": "guarantee",
    "http_idempotency": "guarantee",
    **dict.fromkeys(DATABASE_RENDERER_FEATURES, "behaviour"),
}

DATABASE_CAPABILITY = CapabilitySpec(
    name="database",
    version="v1",
    provider_contract=DatabaseProvider,
    # Major 2 (spec 11): the stores take a ``UnitSession``, ``execution_adapter``
    # left the contract; a driver states ``contract_version = 2``.
    major=2,
    # ``tenant_rls``: the instance keeps PostgreSQL Row-Level Security session
    # settings in sync with the tenant context (opt-in per instance), so a
    # module can require RLS defense in depth for its tables.
    # ``command_idempotency``: command-contract dedupe records commit in the
    # command's own transaction (``DatabaseProvider.command_log()``).
    # ``http_idempotency``: durable ``idempotency_guard(durable=True)`` records
    # commit in the request's own transaction (``DatabaseProvider.request_log()``).
    # ``write_admission``: the driver enforces the request's unit rules at its
    # statement boundary — every write statement detected (conservatively) and
    # admitted for ONE writing unit per request (SANDLOOM-R007), a statement through
    # an exited unit refused (SANDLOOM-R002), the lazy request unit
    # (``LazyUnitOfWorkContract``) and ``has_pending_changes``. The kernel
    # requires it of every database that hosts request units (SANDLOOM-B010).
    feature_kinds=_DATABASE_FEATURE_KINDS,
    description="Relational (transactional) persistence — Unit of Work factory.",
)


__all__ = [
    "DATABASE_CAPABILITY",
    "DATABASE_RENDERER_FEATURES",
    "AuditStore",
    "BrokerUnavailableError",
    "CommandClaim",
    "CommandLogBusy",
    "CommandLogStore",
    "DatabaseProvider",
    "DeliveryStatus",
    "ExecutionAdapter",
    "OutboxHandle",
    "OutboxStore",
    "PublishMany",
    "RelayBatchResult",
    "RequestLogBusy",
    "RequestLogStore",
    "UnitSession",
]
