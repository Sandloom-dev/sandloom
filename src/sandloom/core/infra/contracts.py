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
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, Self

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.descriptors import HealthResult

if TYPE_CHECKING:
    from sandloom.core.infra.config import InstanceConfig


@dataclass(frozen=True)
class OutboxHandle:
    """A backend-agnostic reference to an enqueued outbox event.

    ``id`` is the stable envelope id (also the event id used for dedupe);
    ``raw`` is an opaque backend handle (e.g. the ORM row) the store uses to
    tell a committed row from one undone by a rolled-back SAVEPOINT.
    """

    id: uuid.UUID
    raw: Any = None
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
        session: Any,
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
        session: Any,
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
        session: Any,
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
        session: Any,
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
        session: Any,
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
        session: Any,
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
        session: Any,
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
        session: Any,
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
        self, session: Any, *, dedupe_key: str, status: str, result: str | None
    ) -> None:
        """Store the response on the claimed row (same transaction)."""

    @abc.abstractmethod
    async def release(self, session: Any, *, dedupe_key: str) -> None:
        """Drop the claimed row (same transaction): the request's outcome is
        not to be replayed (a ``5xx`` the endpoint returned)."""

    async def purge_expired(
        self, uow_factory: Callable[[], UnitOfWorkContract], *, limit: int
    ) -> int:
        """Delete up to ``limit`` expired records; return rows deleted."""
        return 0


class InfraProvider(abc.ABC):
    """Base class for every infrastructure provider.

    A provider is one configured *instance* of a *capability*, backed by a
    concrete *driver*. Subclasses set ``capability`` and implement the
    capability-specific surface; lifecycle (``health``/``aclose``) is shared.
    """

    #: Capability key this provider implements (set by the capability subclass).
    capability: ClassVar[str] = "infra"
    #: The contract version a driver was written for: required of a driver
    #: package (refused at load without it, or on another major version).
    contract_version: ClassVar[str]
    #: The capability features this driver supports. The one declaration: the
    #: registry reads it before building, ``features()`` answers from it.
    supported_features: ClassVar[frozenset[str]] = frozenset()
    #: Optional pydantic model validating an instance's options before building.
    options_schema: ClassVar[type[Any] | None] = None

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        self.name = name
        self.driver = driver
        self.options: dict[str, Any] = dict(options)

    @classmethod
    def from_config(cls, config: InstanceConfig) -> Self:
        """Build the provider of one configured instance (the driver factory)."""
        return cls(name=config.name, driver=config.driver, options=config.options)

    @classmethod
    def features_for(cls, options: Mapping[str, Any]) -> frozenset[str]:
        """The features an instance with ``options`` supports, without building
        it. Override only when a feature depends on an option."""
        return cls.supported_features

    def features(self) -> frozenset[str]:
        """Optional capability features THIS instance actually supports.

        A subset of the owning :class:`CapabilitySpec`'s declared features. A
        module may require a feature; the registry resolves against the driver's
        advertised set, so it can refuse a backend that cannot do what the
        module needs (e.g. a graph store without ``cypher``). Default: none.
        """
        return type(self).features_for(self.options)

    async def aopen(self) -> None:
        """Eager initialization hook (optional). Default: no-op.

        Symmetric to :meth:`aclose`. Built-in drivers connect lazily, so this is
        a no-op for them; a driver that must establish a connection pool or warm
        a client up front can override it. Named ``aopen`` (not ``start``) so it
        never collides with a capability's domain verb (e.g.
        ``WorkflowProvider.start``). The registry invokes this for every
        configured instance at startup via ``InfraRegistry.aopen_all()``.
        """
        return None

    async def health(self) -> HealthResult:
        """Liveness probe for this instance. Default: always healthy."""
        return HealthResult(status="ok")

    async def aclose(self) -> None:
        """Release any resources held by this instance (idempotent)."""
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<{type(self).__name__} capability={self.capability!r} "
            f"name={self.name!r} driver={self.driver!r}>"
        )


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

    def has_pending_changes(self, session: Any) -> bool:
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

    def execution_adapter(self) -> Any:
        """PROTOTYPE: the execution-planner adapter of this database.

        ``None`` (the default) means declared operations cannot run here; see
        :class:`sandloom.core.planner.ExecutionAdapter`.
        """
        return None


class Cache(Protocol):
    """What a module calls on ``cache()``: the operations, not the lifecycle."""

    async def get(self, key: str) -> str | None: ...
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None: ...
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int: ...
    async def get_many(self, keys: Sequence[str]) -> list[str | None]: ...
    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None: ...
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def compare_and_delete(self, key: str, expected: str) -> bool: ...
    async def incr_window(
        self, key: str, previous_key: str, *, ttl_seconds: int | None = None
    ) -> tuple[int, str | None]: ...
    async def incr_many(
        self, keys: Sequence[str], *, ttl_seconds: int | None = None
    ) -> list[int]: ...
    async def set_nx_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]: ...
    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str: ...


class CacheProvider(InfraProvider, Cache):
    """Ephemeral key/value store capability (e.g. Redis, Memcached, in-memory).

    The contract is a small set of backend-agnostic *operations* — never the
    raw client. A new backend is plug-compatible the moment it implements these
    five methods; callers cannot reach for engine-specific surface (``eval``,
    ``pipeline``, ``pubsub``, …) and quietly couple to one engine.

    Values are opaque UTF-8 strings; callers serialize structured data
    themselves. TTLs are whole seconds (``None`` = no expiry).
    """

    capability: ClassVar[str] = "cache"

    @abc.abstractmethod
    async def get(self, key: str) -> str | None:
        """Return the value for ``key``, or ``None`` if absent/expired."""

    @abc.abstractmethod
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        """Store ``value`` under ``key`` (overwriting), optionally with a TTL."""

    @abc.abstractmethod
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        """Atomically store ``value`` ONLY if ``key`` is absent (SET NX).

        Returns ``True`` when this call created the key, ``False`` when it
        already existed. The atomic primitive behind reserve-or-conflict
        guards (idempotency keys).
        """

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key`` (no error if it does not exist)."""

    @abc.abstractmethod
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        """Atomically increment the integer at ``key`` and return the new value.

        When the key is first created (result ``1``) and ``ttl_seconds`` is
        given, the expiry is set in the same atomic step — the primitive behind
        fixed-window rate limiting (no counter can leak without a TTL).
        """

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        """Values of ``keys`` in order (``None`` for absent/expired ones).

        Optional batch op (feature ``get_many``): drivers serve it in one round
        trip (Redis ``MGET``); the default falls back to one :meth:`get` each.
        """
        return [await self.get(key) for key in keys]

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        """Store every ``key -> value`` of ``items`` (overwriting), same TTL.

        Optional batch op (feature ``set_many``): drivers serve it in one round
        trip (a Redis pipeline); the default falls back to one :meth:`set` each.
        Not atomic across keys.
        """
        for key, value in items.items():
            await self.set(key, value, ttl_seconds=ttl_seconds)

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Atomically replace ``key`` with ``value`` ONLY if it equals ``expected``.

        Returns ``True`` when swapped, ``False`` when the key is absent or holds
        something else. The primitive behind owner-checked state transitions
        (an idempotency reservation must only be finalised by its owner).
        Optional: drivers advertise it via the ``cas`` feature; the default
        raises ``NotImplementedError`` so callers can detect its absence.
        """
        raise NotImplementedError(
            f"cache driver {self.driver!r} does not support compare_and_set"
        )

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        """Atomically delete ``key`` ONLY if it equals ``expected`` (``cas``)."""
        raise NotImplementedError(
            f"cache driver {self.driver!r} does not support compare_and_delete"
        )

    # ---- optional fused operations (one round trip where the driver can) ----
    # Each has a portable default composed from the core operations above, so
    # every driver supports them; a driver that can do better (one server round
    # trip) overrides them and advertises the matching feature name.

    async def incr_window(
        self, key: str, previous_key: str, *, ttl_seconds: int | None = None
    ) -> tuple[int, str | None]:
        """:meth:`incr` ``key`` and read ``previous_key`` together.

        Returns ``(new count of key, value of previous_key or None)`` — the
        sliding-window-counter rate limiter's per-request step. Feature
        ``incr_window`` = one round trip; the default is two operations.
        """
        count = await self.incr(key, ttl_seconds=ttl_seconds)
        return count, await self.get(previous_key)

    async def incr_many(
        self, keys: Sequence[str], *, ttl_seconds: int | None = None
    ) -> list[int]:
        """:meth:`incr` every key of ``keys`` (TTL set when a key is created).

        Returns the new counts in order. Feature ``incr_many`` = one round trip
        (atomic per key); the default increments the keys one by one.
        """
        return [await self.incr(key, ttl_seconds=ttl_seconds) for key in keys]

    async def set_nx_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        """Store ``value`` ONLY if ``key`` is absent, else return what is there.

        Returns ``(True, None)`` when this call created the key, ``(False,
        existing)`` when it already existed. ``existing`` may be ``None`` when
        the key vanished between the two steps of the portable default (expired
        / deleted concurrently) — callers simply retry. Feature ``set_nx_get``
        = one atomic round trip (Redis ``SET … NX GET``).
        """
        if await self.add(key, value, ttl_seconds=ttl_seconds):
            return True, None
        return False, await self.get(key)

    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        """Store ``value`` ONLY if it is greater than what ``key`` holds.

        A monotonic "set if greater": ``value`` is written when the key is
        absent, holds something that is not a plain decimal (a corrupt value
        is replaced), or holds a plain decimal strictly LOWER than ``value``.
        Values are plain non-negative decimals (``"17"``, ``"1700000000.25"``;
        :func:`parse_plain_decimal`) compared EXACTLY, never as floats — any
        other ``value`` raises ``ValueError``. Returns the value the key holds
        after the call: ``value`` when it was written, else the equal/greater
        one already there. ``ttl_seconds`` applies only when ``value`` is
        written; a winning existing value keeps its own TTL (never shortened,
        never extended).

        Never fails because of concurrent writers: a lost race means another
        writer changed the key, so the decision is simply re-taken. Feature
        ``set_max`` = one atomic round trip (a Lua script on Redis); the
        portable default is a read / ``add`` / ``compare_and_set`` loop
        (:func:`portable_set_max`). **Without the ``cas`` feature that default
        degrades to a plain ``set`` after the read — NOT monotonic**: a value
        a concurrent writer raised in between can be lowered. Callers for whom
        that is unacceptable (the token revocation mark) call
        ``portable_set_max(..., overwrite_without_cas=False)`` on such a
        driver and get ``NotImplementedError`` instead.
        """
        return await portable_set_max(self, key, value, ttl_seconds=ttl_seconds)

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        """Store a subject-state ``value`` ONLY if it ranks above what ``key``
        holds (ADR-0009 item 13: the one subject-state record).

        The value domain is :func:`parse_subject_state`: ``v1:after:<decimal>``
        or ``v1:blocked:<decimal>``. The order is total: every ``blocked``
        value ranks above every ``after`` value (a revocation never unblocks);
        within a kind the decimal instant decides, compared EXACTLY. A stored
        value outside the domain is corrupt and always replaced. Any other
        ``value`` raises ``ValueError``. Returns the value the key holds after
        the call (``value`` when written, else the equal/higher one there);
        ``ttl_seconds`` applies only when ``value`` is written.

        Feature ``subject_state`` = one atomic round trip (a Lua script on
        Redis); the portable default is a read / ``add`` / ``compare_and_set``
        loop (:func:`portable_set_subject_state`) and REFUSES
        (``NotImplementedError``) on a cache without ``compare_and_set``: a
        plain overwrite could unblock a subject or lower a revocation.
        """
        return await portable_set_subject_state(
            self, key, value, ttl_seconds=ttl_seconds
        )

    async def raw_client(self) -> Any:
        """Escape hatch: return the underlying client, if the driver has one.

        NOT part of the portable contract — only backends with a meaningful
        client object (e.g. Redis, for transport-level needs like pub/sub)
        override this. The default raises so portable code never depends on it.
        """
        raise NotImplementedError(
            f"cache driver {self.driver!r} does not expose a raw client"
        )


#: A plain non-negative decimal — the value domain of :meth:`CacheProvider.set_max`
#: (the Redis Lua script matches exactly the same shape).
_PLAIN_DECIMAL = re.compile(r"\s*([0-9]+)(?:\.([0-9]*))?\s*", re.ASCII)

#: Safety bound of :func:`portable_set_max`. Each lost round means ANOTHER
#: writer changed the key in between (monotonic writers only ever raise it),
#: so realistic contention ends in a few rounds; the bound only stops a
#: livelock against a non-monotonic writer (plain ``set`` of lower values).
SET_MAX_MAX_ROUNDS = 1000


class CacheContentionError(RuntimeError):
    """:func:`portable_set_max` lost :data:`SET_MAX_MAX_ROUNDS` races in a row."""


def parse_plain_decimal(raw: str | bytes | None) -> Decimal | None:
    """``raw`` as an exact :class:`~decimal.Decimal` when it is a plain
    non-negative decimal (``[0-9]+(.[0-9]*)?``, surrounding whitespace
    allowed), else ``None`` (absent / corrupt / signed / exponent form)."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    match = _PLAIN_DECIMAL.fullmatch(raw)
    if match is None:
        return None
    whole, frac = match.group(1), match.group(2) or "0"
    return Decimal(f"{whole}.{frac}")


async def portable_set_max(
    provider: Any,
    key: str,
    value: str,
    *,
    ttl_seconds: int | None = None,
    overwrite_without_cas: bool = True,
) -> str:
    """Portable :meth:`CacheProvider.set_max` over ``get`` / ``add`` /
    ``compare_and_set`` (``set`` without ``cas``) — also usable on duck-typed
    cache doubles that implement only those operations.

    Retries for as long as the stored value is below ``value``: a failed
    ``add`` / ``compare_and_set`` means another writer changed the key, so the
    loop re-reads and either finds an equal/greater value (done) or tries
    again. Raises :class:`CacheContentionError` only after
    :data:`SET_MAX_MAX_ROUNDS` lost races in a row.

    On a cache without ``compare_and_set`` (``NotImplementedError``) it
    overwrites with a plain ``set`` — best effort, it can LOWER a value a
    concurrent writer raised in between — unless ``overwrite_without_cas`` is
    false, in which case the ``NotImplementedError`` propagates.
    """
    ours = parse_plain_decimal(value)
    if ours is None:
        raise ValueError(
            f"set_max value must be a plain non-negative decimal: {value!r}"
        )
    for _ in range(SET_MAX_MAX_ROUNDS):
        current = await provider.get(key)
        if current is None:
            if await provider.add(key, value, ttl_seconds=ttl_seconds):
                return value
            continue  # created concurrently: re-read and re-decide
        existing = parse_plain_decimal(current)
        if existing is not None and existing >= ours:
            return current if isinstance(current, str) else current.decode()
        try:
            swapped = await provider.compare_and_set(
                key, current, value, ttl_seconds=ttl_seconds
            )
        except NotImplementedError:
            if not overwrite_without_cas:
                raise
            # Driver without the ``cas`` feature: best-effort (non-monotonic)
            # overwrite.
            await provider.set(key, value, ttl_seconds=ttl_seconds)
            return value
        if swapped:
            return value
        # Lost the race: the key changed under us — re-read and re-decide.
    raise CacheContentionError(
        f"set_max on {key!r} lost {SET_MAX_MAX_ROUNDS} races in a row"
    )


#: The value domain of :meth:`CacheProvider.set_subject_state`.
_SUBJECT_STATE = re.compile(r"v1:(after|blocked):([0-9]+)(?:\.([0-9]*))?", re.ASCII)
#: Kind ranks of the subject-state order (``blocked`` above every ``after``).
SUBJECT_STATE_RANKS: Mapping[str, int] = {"after": 0, "blocked": 1}


def parse_subject_state(raw: str | bytes | None) -> tuple[str, Decimal] | None:
    """``(kind, instant)`` of a subject-state value, else ``None`` (absent or
    corrupt). ``kind`` is ``"after"`` or ``"blocked"``; ``instant`` is the
    exact decimal (unix seconds) the value carries."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    match = _SUBJECT_STATE.fullmatch(raw)
    if match is None:
        return None
    kind, whole, frac = match.group(1), match.group(2), match.group(3) or "0"
    return kind, Decimal(f"{whole}.{frac}")


def _subject_rank(raw: str | bytes | None) -> tuple[int, Decimal] | None:
    parsed = parse_subject_state(raw)
    if parsed is None:
        return None
    return SUBJECT_STATE_RANKS[parsed[0]], parsed[1]


async def portable_set_subject_state(
    provider: Any, key: str, value: str, *, ttl_seconds: int | None = None
) -> str:
    """Portable :meth:`CacheProvider.set_subject_state` over ``get`` / ``add``
    / ``compare_and_set`` (also usable on duck-typed cache doubles).

    Like :func:`portable_set_max` with the subject-state order; a cache
    without ``compare_and_set`` raises ``NotImplementedError`` (never a plain
    overwrite: it could lift a block).
    """
    ours = _subject_rank(value)
    if ours is None:
        raise ValueError(f"not a subject-state value: {value!r}")
    for _ in range(SET_MAX_MAX_ROUNDS):
        current = await provider.get(key)
        if current is None:
            if await provider.add(key, value, ttl_seconds=ttl_seconds):
                return value
            continue
        existing = _subject_rank(current)
        if existing is not None and existing >= ours:
            return current if isinstance(current, str) else current.decode()
        if await provider.compare_and_set(key, current, value, ttl_seconds=ttl_seconds):
            return value
    raise CacheContentionError(
        f"set_subject_state on {key!r} lost {SET_MAX_MAX_ROUNDS} races in a row"
    )


class ObjectStore(Protocol):
    """What a module calls on ``objectstore()``."""

    async def put(
        self,
        key: str,
        data: bytes | Any,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def url(self, key: str) -> str: ...


class ObjectStoreProvider(InfraProvider, ObjectStore):
    """Blob/file storage capability (e.g. local FS, S3, GCS).

    The contract is *key-addressed*: callers supply a logical object ``key`` and
    the object's *content* (bytes or an async byte stream) plus optional
    ``content_type``/``metadata`` — never a filesystem path. The driver maps the
    key to its own namespace safely, so a malicious key can never be treated as
    a host path (no traversal, no symlink escape). This makes S3/GCS/local FS
    interchangeable behind the same surface.
    """

    capability: ClassVar[str] = "objectstore"

    @abc.abstractmethod
    async def put(
        self,
        key: str,
        data: bytes | Any,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Store ``data`` (bytes or an async-readable stream) under ``key``.

        Returns a backend locator for the stored object.
        """

    @abc.abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key`` (raises if absent)."""

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the object stored under ``key``."""

    @abc.abstractmethod
    async def url(self, key: str) -> str:
        """Return a retrieval URL/locator for the object at ``key``."""


class TaskQueue(Protocol):
    """What a module calls on ``taskqueue()``."""

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...


class TaskQueueProvider(InfraProvider, TaskQueue):
    """Background job broker capability (e.g. Celery, RQ, Dramatiq, in-memory).

    The contract is the single operation callers actually need — *enqueue a
    named task with a payload* — never the underlying broker/app object. A new
    broker is plug-compatible the moment it implements :meth:`enqueue`; callers
    cannot couple to Celery's ``send_task``/``.delay`` surface.
    """

    capability: ClassVar[str] = "taskqueue"

    @abc.abstractmethod
    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Enqueue ``task`` (by registered name) with keyword ``payload``.

        ``delay_seconds`` schedules the task for later. With ``idempotency_key``
        (feature ``idempotency_key``), successful repeats return the original job
        id within the driver's replay window; different arguments conflict.
        Concurrent or ambiguous sends fail closed instead of publishing again.
        This requires a shared durable dedupe store and does NOT deduplicate
        broker redelivery or task effects. Returns the broker's task/job id.
        """

    async def raw_backend(self) -> Any:
        """Escape hatch: return the underlying broker/app, if the driver has one.

        NOT part of the portable contract — only backends with a meaningful app
        object (e.g. Celery) override this. The default raises so portable code
        never depends on it.
        """
        raise NotImplementedError(
            f"taskqueue driver {self.driver!r} does not expose a raw backend"
        )


class EventBusProvider(InfraProvider):
    """Pub/sub domain-event transport capability.

    Wraps an object implementing ``sandloom.core.eventing.event_bus.EventBusProtocol``
    so the
    bus participates in the same registry/lifecycle as other infrastructure.
    """

    capability: ClassVar[str] = "eventbus"

    @abc.abstractmethod
    def bus(self) -> Any:
        """Return the underlying event bus (EventBusProtocol)."""


#: Outbound delivery channels a notification can target.
NotificationChannel = Literal["email", "sms", "push", "inapp"]


@dataclass(frozen=True)
class Notification:
    """One outbound notification, independent of the delivery backend.

    A logical message: which ``channel`` to deliver on, the ``recipient``
    address for that channel (an email, phone, device token, or user id for
    ``inapp``), a short ``subject`` and ``body``, plus structured ``data`` and an
    optional ``dedupe_key`` a driver may use to suppress duplicate sends.

    ``body_fields`` are the values the ``body`` was built from, so a driver can
    render its own template. They are as sensitive as the body (a secret link, a
    name): a driver never logs them, and ``data`` stays safe to record.
    """

    channel: NotificationChannel
    recipient: str
    subject: str
    body: str
    data: Mapping[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    body_fields: Mapping[str, str] = field(default_factory=dict, repr=False)


class Notifier(Protocol):
    """What a module calls on ``notifier()``."""

    async def send(self, notification: Notification) -> None: ...


class NotifierProvider(InfraProvider, Notifier):
    """Outbound notification capability (email, SMS, push, in-app).

    Modules never import an email/SMS SDK: they build a :class:`Notification` and
    call :meth:`send`. A driver delivers it via its backend (SMTP, a provider
    API, a structured log in dev). The portable surface is a single ``send`` —
    drivers advertise which channels they actually deliver via :meth:`features`.
    """

    capability: ClassVar[str] = "notification"

    @abc.abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver ``notification`` (raises on a delivery failure)."""


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
    "RelationalProvider",
    "CacheProvider",
    "CacheContentionError",
    "SET_MAX_MAX_ROUNDS",
    "parse_plain_decimal",
    "portable_set_max",
    "ObjectStoreProvider",
    "TaskQueueProvider",
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
