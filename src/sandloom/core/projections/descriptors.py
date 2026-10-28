"""Descriptors for the Cross-Module Query / Projection layer.

A *query projection* is a denormalised read model — a table a query/read module
owns and keeps up to date asynchronously from OTHER modules' domain events, so
a UI list that logically spans several modules is served by ONE indexed read
instead of a forbidden cross-module SQL join. The owning module declares:

* a :class:`QueryProjectionDescriptor` — what the projection is, which database
  instance stores it, which source event channels feed it, and that it is
  rebuildable (the projection is a cache; the source modules remain the truth);
* one :class:`ProjectionConsumerDescriptor` per source channel — the durable
  consumer that applies a delivered event to the projection.

These are pure metadata (frozen dataclasses) registered through ``ModuleAPI`` and
preflighted at module load, exactly like event schemas: nothing in core imports
a business module, and a half-registered module leaves no orphan behind.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sandloom.core.consumers.callbacks import (
    EventEnvelope,
    consumer_group_name,
    validate_consumer_identity,
)
from sandloom.core.eventing.delivery import START_FROM_EARLIEST

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

# A projection consumer handler receives the read-model's UoW session (already
# opened on the projection's storage database, with the event durably claimed
# when ``durable``) and the delivered envelope. It applies the change and
# returns; raising triggers redelivery (at-least-once) instead of a silent drop.
ProjectionHandler = Callable[["AsyncSession", EventEnvelope], Awaitable[None]]
#: ``purge(session, cutoff) -> rows deleted``.
TombstonePurger = Callable[[Any, datetime], Awaitable[int]]
#: ``token_entries(channel, payload) -> [(row key, group, aggregate id,
#: version), ...]``: ``group`` is the source group's version column.
TokenEntries = Callable[
    [str, Mapping[str, Any]], Iterable[tuple[Mapping[str, Any], str, Any, int]]
]


@dataclass(frozen=True, slots=True)
class QueryProjectionDescriptor:
    """A denormalised cross-module read model owned by one module.

    ``consistency`` is intentionally fixed to ``"eventual"``: a projection lags
    its source modules by the event delivery + apply time and must never back a
    strong-consistency command invariant (validate those against the owner's
    public command contract instead). ``rebuildable`` records that the
    projection can be dropped and reconstructed from the source modules — the
    projection is a cache, not a source of truth.
    """

    name: str
    owner_module: str
    version: str = "v1"
    # Source event channels (``<module>.<entity>.<action>.vN``) this projection
    # is built from — documentation + the contract the rebuilder/consumers honor.
    source_events: tuple[str, ...] = ()
    # Logical database instance that stores the projection table (the owning
    # module declares the matching ``CapabilityRequirement("database", ...)``).
    storage: str = "default"
    consistency: Literal["eventual"] = "eventual"
    rebuildable: bool = True
    # Deletes the tombstones older than a cutoff, in a unit on ``storage``;
    # returns how many (``purge_tombstones``, spec §6.4 item 5). A projection
    # with one also gets the purge-watermark check on its apply path.
    purge: TombstonePurger | None = None
    # The ``(row key, group, aggregate id, version)`` entries an event of a
    # source channel will produce: what a write's consistency token carries
    # (spec §6.4 item 7).
    token_entries: TokenEntries | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("QueryProjectionDescriptor.name must be non-empty")
        if not self.owner_module:
            raise ValueError(
                f"projection {self.name!r}: owner_module must be non-empty"
            )


@dataclass(frozen=True, slots=True)
class ProjectionConsumerDescriptor:
    """A durable consumer that applies one source channel to one projection.

    The durable consumer-group id on the event bus is :attr:`group_name`
    (``<module>:<name>`` when registered through ``ModuleAPI``; the module is
    stamped by the loader): each consumer gets its OWN group, so it has its own
    pending list, redelivery and dead-letter queue, independent of every other
    subscriber to the channel. Keep ``name`` stable across deploys — changing it
    makes the bus treat the consumer as brand new (a new group starting at
    ``start_from``, default the channel's retained backlog). ``group`` pins an
    explicit group id instead.

    ``durable`` (default) claims each event id in the handler's Unit of Work
    (``UNIQUE(consumer, event_id)``) so the apply runs exactly once even though
    delivery is at-least-once; idempotent handlers may set it ``False`` and rely
    on the monotonic upsert alone.

    ``required=True`` makes the projection subscription part of readiness. Use
    ``False`` only when the owning read path explicitly tolerates a stale or
    unavailable projection.
    """

    name: str
    source_channel: str
    projection: str
    handler: ProjectionHandler
    durable: bool = True
    required: bool = True
    module: str | None = None
    group: str | None = None
    start_from: str = START_FROM_EARLIEST
    bind_tenant_from: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ProjectionConsumerDescriptor.name must be non-empty")
        if not callable(self.handler):
            raise TypeError(f"consumer {self.name!r}: handler must be callable")
        validate_consumer_identity(
            "projection consumer",
            self.name,
            self.group,
            self.start_from,
            self.bind_tenant_from,
        )

    @property
    def group_name(self) -> str:
        """The durable consumer-group id (also the exactly-once inbox key)."""
        return consumer_group_name(self.name, self.module, self.group)


@dataclass(slots=True)
class ProjectionRebuildResult:
    """Outcome of a rebuild run (rows written + whether the index was cleared)."""

    projection: str
    rows: int = 0
    cleared: bool = False
    details: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "EventEnvelope",
    "ProjectionHandler",
    "QueryProjectionDescriptor",
    "ProjectionConsumerDescriptor",
    "ProjectionRebuildResult",
    "TokenEntries",
    "TombstonePurger",
]
