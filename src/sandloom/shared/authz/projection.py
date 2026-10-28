"""Authorization projections — the ``projection`` resolution mode's plumbing.

A consuming module that needs an authz fact on a hot path (org membership,
plan/feature) can maintain a local, denormalised read model built from the
owner modules' events — the sanctioned cross-module read pattern, specialised
to access. It then gates on an :class:`~sandloom.shared.authz.atoms_fact.Entitled`
fact whose ``projection_reader`` runs one indexed SELECT against that table:
no RPC, identical in monolith and split, resilient to the owner being down.

This module keeps SQL out of ``shared``: the projection model, the consumer
handlers and the ``projection_reader`` are all supplied BY THE MODULE (it owns
the table). Here we provide:

- :func:`register_authz_projection` — register projection + consumers +
  rebuilder in one call, optionally installing a CRITICAL rebuild gate.
- :func:`make_rebuild_gate` — the startup task that reconstructs the projection
  so a freshly-deployed consumer never fail-closed-denies on an empty table.

Eventual consistency is bounded by event lag, NOT token TTL, and is fresher
than a claim for revocations. It is still eventually consistent — declare
``min_freshness="live"`` on any fact backing a hard/money invariant so config
can never downgrade it to a projection.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("sandloom.access.projection")

StartupTask = Callable[[], Awaitable[None]]


def make_rebuild_gate(rebuilder: Any, *, name: str = "") -> StartupTask:
    """A startup task that rebuilds an authz projection from its sources.

    Captures the ``rebuilder`` supplied at registration in its closure (no
    process-global / registry lookup at run time). Install as ``critical=True``
    so the process refuses traffic until the projection is populated — otherwise
    an empty table makes every fact resolve False and the consumer
    fail-closed-denies all callers. The rebuilder must be idempotent (the
    protocol guarantees it), so re-running on boot is safe; for large
    projections prefer ``sandloom projection rebuild <name>`` in the one-shot migrate
    step with ``rebuild_on_startup=False``.
    """

    async def _gate() -> None:
        result = await rebuilder.rebuild_all()
        logger.info(
            "authz projection %s rebuilt: %s rows",
            name or getattr(rebuilder, "__class__", type(rebuilder)).__name__,
            getattr(result, "rows", "?"),
        )

    return _gate


def register_authz_projection(
    core: Any,
    *,
    descriptor: Any,
    consumers: list[Any],
    rebuilder: Any,
    rebuild_on_startup: bool = False,
    critical: bool = True,
) -> None:
    """Register an authorization projection, its consumers and rebuilder.

    ``descriptor`` is a ``QueryProjectionDescriptor``; ``consumers`` a list of
    ``ProjectionConsumerDescriptor`` (one per source channel); ``rebuilder`` a
    ``ProjectionRebuilder``. When ``rebuild_on_startup`` is True a rebuild gate
    is installed (``critical`` controls whether its failure aborts startup).
    """
    core.register_query_projection(descriptor)
    for consumer in consumers:
        core.register_projection_consumer(consumer)
    core.register_projection_rebuilder(descriptor.name, rebuilder)
    if rebuild_on_startup:
        core.add_startup_task(
            make_rebuild_gate(rebuilder, name=descriptor.name), critical=critical
        )
