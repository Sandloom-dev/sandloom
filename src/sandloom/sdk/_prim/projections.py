"""New L2 projection primitives of ``sandloom.sdk.projections`` (spec §2.3, §6.4).

The store protocol a non-relational projection target implements, the
consistency token and the operations on a projection
(``sandloom.core.projections.operations``).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sandloom.core.projections import (
    ProjectionConsumerDescriptor,
    ProjectionRebuildResult,
    QueryProjectionDescriptor,
    monotonic_delete,
    monotonic_upsert,
)
from sandloom.core.projections.operations import (
    purge_projection_tombstones,
    read_consistency_token,
    rebuild_projection,
)
from sandloom.core.projections.stores import ConditionalWriteStore
from sandloom.core.projections.tokens import ConsistencyToken


def read_token(header: str | None) -> ConsistencyToken | None:
    """The token of ``header``; ``None`` for a missing, malformed, expired,
    unknown-kid or other-tenant token (never an error)."""
    return read_consistency_token(header)


async def rebuild(
    name: str, *, tenant_id: UUID | None = None, blue_green: bool = True
) -> ProjectionRebuildResult:
    """Rebuild projection ``name`` from its source (§6.4): one tenant, or
    every tenant. Purges refuse while it runs; the purge watermark is kept.
    Without an alias-swapping store a blue/green rebuild runs in place
    (``details["blue_green"] == "in_place"``)."""
    return await rebuild_projection(name, tenant_id=tenant_id, blue_green=blue_green)


async def purge_tombstones(name: str, *, older_than: timedelta) -> int:
    """Purge tombstones behind the watermark that refuses older replays."""
    return await purge_projection_tombstones(name, older_than=older_than)


__all__ = [
    "ConditionalWriteStore",
    "ConsistencyToken",
    "ProjectionConsumerDescriptor",
    "ProjectionRebuildResult",
    "QueryProjectionDescriptor",
    "monotonic_delete",
    "monotonic_upsert",
    "purge_tombstones",
    "read_token",
    "rebuild",
]
