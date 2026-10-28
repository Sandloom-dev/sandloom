"""Monotonic (version-guarded) write helpers for projection consumers.

Event delivery is at-least-once and NOT ordered per entity: a consumer can see
``project.updated`` events out of order, and the same event more than once. A
naive ``UPDATE`` would let an older event clobber a newer one. These helpers
make a projection write apply only when the incoming source version is newer
than what the row already holds — so duplicates are no-ops and out-of-order
deliveries never roll a row backwards. Each source the row denormalises carries
its OWN version column (e.g. ``task_version``, ``project_version``).

A stored version of ``NULL`` (a column added later, or a row seeded without
that source) counts as "older than any version": the first event for that
source always applies (``NULL < v`` would otherwise be NULL/false forever and
the column could never be updated).

Tenancy: these helpers issue Core statements against the table, which the ORM
tenant guard (``do_orm_execute``) does not see. They therefore enforce tenant
ownership themselves for tables with a ``tenant_id`` column:

* ``monotonic_upsert`` requires the row's ``tenant_id``; it must match the
  bound tenant (if one is bound and not ``unscoped()``), and the on-conflict
  update only touches an existing row OF THE SAME TENANT — a key collision with
  another tenant's row is never overwritten;
* ``monotonic_update`` is auto-scoped to the bound tenant when one is bound.
  Without a bound tenant it is a SYSTEM fan-out keyed by the ``match`` columns
  (e.g. a globally-unique source id, as projection consumers do) — include
  ``tenant_id`` in ``match`` whenever the source event carries it.

Postgres-specific (``INSERT ... ON CONFLICT``) — the projection store. A
projection's owning module imports these from core, so it never reaches into a
concrete backend driver itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Table, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult

from sandloom.core.tenant_context import (
    TenantMismatchError,
    _bypass,
    current_tenant_id_optional,
    unscoped,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession


def _table_of(target: Any) -> Table:
    """Accept a Core ``Table`` or an ORM-mapped class and return its table."""
    return cast(Table, getattr(target, "__table__", target))


def _older_than(table: Table, version_column: str, incoming_version: int) -> Any:
    """``stored IS NULL OR stored < incoming`` — NULL counts as oldest."""
    column = table.c[version_column]
    return or_(column.is_(None), column < incoming_version)


def _bound_tenant() -> Any:
    """The bound tenant, or ``None`` when unbound or inside ``unscoped()``."""
    if _bypass.get():
        return None
    return current_tenant_id_optional()


async def monotonic_upsert(
    session: AsyncSession,
    target: Any,
    *,
    key: dict[str, Any],
    values: dict[str, Any],
    version_column: str,
    incoming_version: int,
    insert_only: dict[str, Any] | None = None,
) -> bool:
    """Insert-or-update a single projection row, guarded by source version.

    Inserts the row (``key`` + ``values`` + ``insert_only`` + the version) when
    absent; on a primary-key/unique conflict it updates ``values`` and the
    version ONLY when the stored ``version_column`` is strictly older than
    ``incoming_version`` (or NULL). A duplicate (equal version) or an
    out-of-order older event leaves the row untouched. Returns ``True`` when a
    row was inserted or updated.

    ``insert_only`` columns are written on INSERT but NEVER touched by the
    on-conflict update — for fields this source merely SEEDS but does not own
    (e.g. a task upsert seeds the denormalised project name once, but project
    events, guarded by their own version, are the authority for keeping it
    fresh). Without this, every task update would clobber project fields back to
    whatever the seed lookup returned, rolling back fresher project data.
    """
    table = _table_of(target)
    seed = insert_only or {}
    row = {**key, **values, **seed, version_column: incoming_version}
    where = _older_than(table, version_column, incoming_version)
    if "tenant_id" in table.c:
        row_tenant = row.get("tenant_id")
        if row_tenant is None:
            raise ValueError(
                f"monotonic_upsert into tenant-owned {table.name!r} requires "
                "tenant_id in key/values/insert_only"
            )
        bound = _bound_tenant()
        if bound is not None and row_tenant != bound:
            raise TenantMismatchError(
                f"projection row tenant_id does not match the bound tenant "
                f"({table.name})"
            )
        # Never overwrite another tenant's row that happens to share the key.
        where = where & (table.c["tenant_id"] == row_tenant)
    stmt = pg_insert(table).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=list(key.keys()),
        set_={**values, version_column: incoming_version},
        where=where,
    )
    # Trusted kernel path: tenant/version ownership was checked above. Core
    # SQL cannot receive ORM loader criteria; RLS remains enabled underneath.
    with unscoped():
        result = cast("CursorResult[Any]", await session.execute(stmt))
    return bool(result.rowcount)


async def monotonic_update(
    session: AsyncSession,
    target: Any,
    *,
    match: dict[str, Any],
    values: dict[str, Any],
    version_column: str,
    incoming_version: int,
) -> int:
    """Update every projection row matching ``match``, guarded by source version.

    For denormalised fields that fan out across many rows (e.g. a project's name
    copied onto every task row): updates the matched rows whose stored
    ``version_column`` is strictly older than ``incoming_version`` (or NULL),
    setting the new ``values`` and version. Out-of-order/duplicate project
    events are no-ops. Returns the number of rows updated. Auto-scoped to the
    bound tenant (if any) on tenant-owned tables — see the module docstring.

    Note: this only refreshes rows that ALREADY EXIST and are COMMITTED (or
    locked by this transaction). A row another writer inserted but has not
    committed yet is invisible here, so a writer that reads the source over RPC
    and then inserts can lose this update for good. Serialise the two writers on
    a local, version-guarded snapshot row of the source (upsert it here first;
    ``SELECT ... FOR UPDATE`` it in the other writer and copy its values) — see
    the projecthub taskboard ``ProjectSnapshot``.
    """
    table = _table_of(target)
    if "tenant_id" in table.c and "tenant_id" in values:
        raise ValueError("monotonic_update cannot change tenant_id")
    conditions = [table.c[col] == val for col, val in match.items()]
    conditions.append(_older_than(table, version_column, incoming_version))
    if "tenant_id" in table.c:
        bound = _bound_tenant()
        if bound is not None:
            if "tenant_id" in match and match["tenant_id"] != bound:
                raise TenantMismatchError(
                    "projection match tenant_id does not match the bound tenant"
                )
            conditions.append(table.c["tenant_id"] == bound)
    stmt = (
        update(table)
        .where(*conditions)
        .values(**values, **{version_column: incoming_version})
    )
    with unscoped():
        result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def monotonic_delete(
    session: AsyncSession,
    target: Any,
    *,
    key: dict[str, Any],
    version_column: str,
    incoming_version: int,
    values: dict[str, Any] | None = None,
    insert_only: dict[str, Any] | None = None,
    deleted_column: str = "deleted_at",
    deleted_at: datetime | None = None,
) -> bool:
    """Persist a versioned tombstone, including when deletion arrives first.

    A physical DELETE discards the version and lets a delayed older upsert
    resurrect the row. This helper retains it using the same tenant/version
    checks as ``monotonic_upsert``. The projection needs a nullable timestamp
    ``deleted_column`` and reads must filter it to NULL. ``values`` and
    ``insert_only`` must supply tenant_id and other required columns so an
    absent row can be inserted as a tombstone. Only an explicitly newer event
    can subsequently clear it by upserting ``deleted_column=None``.

    Tombstones must survive at least the complete replay/rebuild horizon;
    removing them early reintroduces resurrection. Rebuilders apply source
    versions through the same helper and must not infer deletion from an
    incomplete source listing.
    """
    table = _table_of(target)
    if deleted_column not in table.c:
        raise ValueError(
            f"Projection {table.name!r} has no tombstone column {deleted_column!r}"
        )
    if deleted_column in (insert_only or {}):
        raise ValueError("insert_only cannot override the deletion tombstone")
    timestamp = deleted_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    timestamp = timestamp.astimezone(UTC)
    if not getattr(table.c[deleted_column].type, "timezone", False):
        timestamp = timestamp.replace(tzinfo=None)
    return await monotonic_upsert(
        session,
        target,
        key=key,
        values={**(values or {}), deleted_column: timestamp},
        version_column=version_column,
        incoming_version=incoming_version,
        insert_only=insert_only,
    )


__all__ = ["monotonic_upsert", "monotonic_update", "monotonic_delete"]
