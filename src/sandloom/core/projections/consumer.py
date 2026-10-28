"""Runtime that wires registered projection consumers to the event bus.

A projection consumer is a SPECIALISATION of the generic durable event consumer
(``sandloom.core.consumers.callbacks``): same UoW + exactly-once + observability
wrapper, plus the projection-registry linkage (its database is the projection's
storage instance, and it carries projection labels). At startup
``start_projection_consumers(core)`` subscribes each one under its own durable
group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sandloom.core.consumers.callbacks import (
    SessionOpener,
    _projection_callback,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade
    from sandloom.core.projections.descriptors import ProjectionConsumerDescriptor

import logging

import sandloom.core.consumers.runtime as _runtime

logger = logging.getLogger("sandloom.core.projections")


def make_consumer_callback(
    descriptor: ProjectionConsumerDescriptor,
    storage: str,
    opener: SessionOpener,
) -> Any:
    """Wrap a projection consumer descriptor into an event-bus callback.

    Delegates to the generic consumer wrapper with projection labels/metrics.
    Exposed separately so it can be unit-tested without a live bus.
    """
    return _projection_callback(descriptor, storage, opener, descriptor.group_name)


async def start_projection_consumers(
    core: CoreFacade,
    *,
    session_opener: SessionOpener | None = None,
) -> int:
    """Subscribe every registered projection consumer to the event bus.

    Returns the number of consumers wired through the canonical supervised
    runtime. Duplicate groups and missing required projections fail startup;
    repeated calls do not add duplicate handlers. ``session_opener`` defaults
    to ``unit_of_work.db_session`` and is injectable for tests.
    """

    snapshot = await _runtime._start_compatible_runtime(core, session_opener)
    return sum(status.kind == "projection" for status in snapshot.consumers)


__all__ = ["start_projection_consumers", "make_consumer_callback"]
