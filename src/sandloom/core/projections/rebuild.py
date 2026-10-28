"""Projection rebuild contract.

A projection is a cache: it must be reconstructable from the source modules at
any time (after corruption, a schema migration, or a backfill of a brand-new
projection). Because modules are isolated, a rebuild MUST read the source
modules through their public read contracts / events — never their private
tables. The owning module supplies a :class:`ProjectionRebuilder`; the registry
holds it so the ``sandloom projection rebuild`` CLI and ops tooling can invoke it by
projection name without knowing the module.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterable, Iterable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sandloom.core.projections.descriptors import ProjectionRebuildResult
from sandloom.core.projections.purge import WatermarkStore, rebuilding

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.projections.registry import QueryProjectionRegistry


@runtime_checkable
class ProjectionRebuilder(Protocol):
    """Reconstructs a projection from its source modules (idempotently).

    Every method rebuilds a SCOPE and returns a
    :class:`ProjectionRebuildResult`. Implementations re-read source state via
    public contracts/events and apply it through the same monotonic upsert the
    live consumers use, so a rebuild and steady-state delivery converge to the
    same rows. Rebuilds must be safe to run while consumers are live. Source
    deletions require versioned tombstones (``monotonic_delete``), never
    physical DELETEs: a stale source row observed before a concurrent delete
    must not resurrect a row during the rebuild. Retain tombstones through
    the whole replay/rebuild horizon and distinguish complete snapshots from
    partial listings before reconciling absent entities.
    """

    async def rebuild_all(self) -> ProjectionRebuildResult:
        """Rebuild the whole projection (every tenant)."""
        ...

    async def rebuild_tenant(self, tenant_id: uuid.UUID) -> ProjectionRebuildResult:
        """Rebuild only the rows belonging to ``tenant_id``."""
        ...

    async def rebuild_entity(self, ref: str) -> ProjectionRebuildResult:
        """Rebuild a single entity (a stable ``owner_module:entity_id`` ref)."""
        ...


class ProjectionRebuildError(LookupError):
    """No rebuilder for the projection, or an invalid rebuild scope."""


async def rebuild_tenants(
    rebuilder: ProjectionRebuilder,
    projection: str,
    tenants: Iterable[uuid.UUID] | AsyncIterable[uuid.UUID],
) -> ProjectionRebuildResult:
    """``rebuild_all`` over an explicit tenant source: one
    ``rebuild_tenant`` per tenant (each idempotent and convergent with the
    live consumers), the row counts summed. The source is the caller's: the
    kernel cannot enumerate an app's tenants (the tenancy contract, or the
    tenants the projection already holds, are the usual ones)."""
    rows, seen = 0, 0
    if isinstance(tenants, AsyncIterable):
        async for tenant in tenants:
            rows += (await rebuilder.rebuild_tenant(tenant)).rows
            seen += 1
    else:
        for tenant in tenants:
            rows += (await rebuilder.rebuild_tenant(tenant)).rows
            seen += 1
    return ProjectionRebuildResult(
        projection=projection, rows=rows, details={"tenants": seen}
    )


async def rebuild(
    registry: QueryProjectionRegistry,
    name: str,
    *,
    tenant_id: uuid.UUID | None = None,
    entity: str | None = None,
    watermarks: WatermarkStore | None = None,
    blue_green: bool = False,
) -> ProjectionRebuildResult:
    """Rebuild projection ``name`` — one entity (``entity``, a stable
    ``owner_module:entity_id`` ref), one tenant, or everything — through its
    registered rebuilder (spec §6.4 item 7).

    With ``watermarks`` the rebuild is marked in progress for its duration
    (a tombstone purge refuses meanwhile) and the purge watermark is left as
    it is: a rebuild never resets or lowers it. ``blue_green=True`` (a fresh
    physical target, its consumers started before the source scan, then an
    atomic alias swap) is the store driver's: the in-place rebuild here is
    convergent because every write is version-guarded."""
    if blue_green:
        raise ProjectionRebuildError(
            f"projection {name!r}: blue/green rebuilds need a store with alias "
            "swap support; run rebuild(..., blue_green=False) (in place, "
            "version-guarded)"
        )
    if tenant_id is not None and entity is not None:
        raise ProjectionRebuildError("rebuild one tenant OR one entity, not both")
    rebuilder = registry.get_rebuilder(name)
    if rebuilder is None:
        raise ProjectionRebuildError(f"no rebuilder registered for projection {name!r}")

    async def _run() -> Any:
        if entity is not None:
            return await rebuilder.rebuild_entity(entity)
        if tenant_id is not None:
            return await rebuilder.rebuild_tenant(tenant_id)
        return await rebuilder.rebuild_all()

    if watermarks is None:
        return await _run()  # type: ignore[no-any-return]
    async with rebuilding(name, watermarks):
        return await _run()  # type: ignore[no-any-return]


__all__ = [
    "ProjectionRebuildError",
    "ProjectionRebuilder",
    "rebuild",
    "rebuild_tenants",
]
