"""Cross-Module Query / Projection layer (public surface for read modules).

A read/query module imports everything it needs from here:

* descriptors it registers via ``ModuleAPI`` (``QueryProjectionDescriptor``,
  ``ProjectionConsumerDescriptor``);
* the ``ProjectionRebuilder`` contract it implements;
* the version-guarded write helpers its consumers apply
  (``monotonic_upsert`` / ``monotonic_update``);
* the ``EventEnvelope`` type for handler signatures.

It never needs the event-bus internals or a concrete database driver — keeping
projection modules inside the module-isolation rules.
"""

from __future__ import annotations

from sandloom.core.projections.consumer import (
    make_consumer_callback,
    start_projection_consumers,
)
from sandloom.core.projections.descriptors import (
    EventEnvelope,
    ProjectionConsumerDescriptor,
    ProjectionHandler,
    ProjectionRebuildResult,
    QueryProjectionDescriptor,
)
from sandloom.core.projections.rebuild import ProjectionRebuilder
from sandloom.core.projections.registry import (
    ProjectionRegistryError,
    QueryProjectionRegistry,
)
from sandloom.core.projections.upsert import (
    monotonic_delete,
    monotonic_update,
    monotonic_upsert,
)

__all__ = [
    "QueryProjectionDescriptor",
    "ProjectionConsumerDescriptor",
    "ProjectionHandler",
    "ProjectionRebuildResult",
    "ProjectionRebuilder",
    "EventEnvelope",
    "QueryProjectionRegistry",
    "ProjectionRegistryError",
    "monotonic_upsert",
    "monotonic_update",
    "monotonic_delete",
    "make_consumer_callback",
    "start_projection_consumers",
]
