"""The ``database`` capability's spec (the relational paradigm; spec section 11).

Its features and their kinds live here, beside the other capabilities' specs,
with the provider ABC and :class:`UnitSession`, the kernel's view of a unit's
session that the stores and :meth:`DatabaseProvider.has_pending_changes` take.
The stores the ABC returns (outbox, audit, command and request logs) are in
:mod:`sandloom.core.infra.contracts`, which re-exports the ABC.

The driver door may name SQLAlchemy types (the kernel's relational surface
already does): ``MetaData`` below is imported for type checking only.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, FeatureKind, InfraProvider

if TYPE_CHECKING:
    from sqlalchemy import MetaData

    from sandloom.core.contracts.uow import UnitOfWorkContract
    from sandloom.core.infra.contracts import (
        AuditStore,
        CommandLogStore,
        OutboxStore,
        RequestLogStore,
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
    "DatabaseProvider",
    "UnitSession",
]
