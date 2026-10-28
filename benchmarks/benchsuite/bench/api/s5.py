"""S5 endpoints — the secure SaaS read in four execution variants.

JWT (``get_current_user_id``), ``TenantScope()``, ``require(Subscribed(of=Account.user()))``
(claim-cheap) and a ``TenantScoped`` row under PostgreSQL RLS. ``/s5`` is the
request UoW, ``/s5ro`` the
declared read-only session, ``/s5fast`` / ``/s5fn`` the raw-driver fast paths
(see ``benchsuite.bench.fastread``), ``/s5plan`` the declared ``ReadOperation``
through the execution planner (``benchsuite.bench.operations``).
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from sandloom.core.di import (
    DbSession,
    PlannedReads,
    ReadOnlyDbSession,
    TenantScope,
    get_core,
)
from sandloom.shared.authz import require
from sandloom.shared.guards.auth import get_current_user_id

from ... import common, compat
from .. import fastread
from ..operations import get_tenant_item, list_tenant_items
from ..tenant_models import BenchTenantItem
from . import ItemOut

# --- S5: secure tenant read (JWT + tenant + subscription gate + RLS) -------- #
s5_router = APIRouter(
    tags=["bench-s5"], dependencies=[Depends(require(compat.subscribed()))]
)
Tenant = Annotated[UUID, TenantScope()]
UserId = Annotated[UUID, Depends(get_current_user_id)]


async def _engine(core: Any = Depends(get_core)) -> Any:  # async: no thread hop
    return core.database("default").engine


Engine = Annotated[Any, Depends(_engine)]


def _out(row: Any) -> ItemOut:
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=row[0], name=row[1], value=row[2])


@s5_router.get("/s5/{item_id}", response_model=ItemOut)
async def s5(item_id: int, user_id: UserId, tenant: Tenant, db: DbSession) -> ItemOut:
    """The request UoW."""
    item = await db.get(BenchTenantItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@s5_router.get(
    "/s5bud/{item_id}",
    response_model=ItemOut,
    dependencies=[
        *compat.legacy_marker("operation_class", "R"),
        *compat.legacy_marker("revocation_freshness", max_staleness_s=5),
    ],
)
async def s5_budgeted(
    item_id: int, user_id: UserId, tenant: Tenant, db: DbSession
) -> ItemOut:
    """``/s5`` as an ordinary class-R read with a 5 s revocation budget: a
    per-process copy of the revocation mark may answer (the block mark is
    still read live, one ``GET``)."""
    item = await db.get(BenchTenantItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@s5_router.get("/s5ro/{item_id}", response_model=ItemOut)
async def s5_read_only(
    item_id: int, user_id: UserId, tenant: Tenant, db: ReadOnlyDbSession
) -> ItemOut:
    """(c) the S5 handler with the session DECLARED read-only."""
    item = await db.get(BenchTenantItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@s5_router.get("/s5fast/{item_id}", response_model=ItemOut)
async def s5_fast(
    item_id: int, user_id: UserId, tenant: Tenant, engine: Engine
) -> ItemOut:
    """(d) raw asyncpg: BEGIN READ ONLY + set_config merged | SELECT | COMMIT."""
    return _out(
        await fastread.fetch_tx(engine, tenant, fastread.SQL_BY_ID, item_id, tenant)
    )


@s5_router.get("/s5fn/{item_id}", response_model=ItemOut)
async def s5_fn(
    item_id: int, user_id: UserId, tenant: Tenant, engine: Engine
) -> ItemOut:
    """(d') one round trip through the plpgsql ``bench_s5_get`` (autocommit)."""
    return _out(await fastread.fetch_fn(engine, tenant, item_id))


@s5_router.get("/s5plan/{item_id}", response_model=ItemOut)
async def s5_planned(
    item_id: int, user_id: UserId, tenant: Tenant, reads: PlannedReads
) -> ItemOut:
    """(e) the DECLARED operation through the execution planner: ORM general
    executor, or with PLANNER_OPTIMIZED_EXECUTOR=true the driver-level
    optimized one (BEGIN READ ONLY + tenant GUC | SELECT | COMMIT)."""
    item = await reads.one(get_tenant_item, id=item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return item


def _partner(item_id: int) -> int:
    """The second row of the two-read routes: the next id of the same tenant."""
    n = common.S5_ROWS_PER_TENANT
    return (item_id - 1) // n * n + item_id % n + 1


@s5_router.get("/s5two/{item_id}", response_model=ItemOut)
async def s5_two(
    item_id: int, user_id: UserId, tenant: Tenant, db: DbSession
) -> ItemOut:
    """TWO reads on the request UoW (the ORM twin of ``/s5plan2``)."""
    item = await db.get(BenchTenantItem, item_id)
    other = await db.get(BenchTenantItem, _partner(item_id))
    if item is None or other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@s5_router.get("/s5plan2/{item_id}", response_model=ItemOut)
async def s5_planned_two(
    item_id: int, user_id: UserId, tenant: Tenant, reads: PlannedReads
) -> ItemOut:
    """TWO declared reads in one PlannedReads route: on the general executor
    they share ONE read-only transaction (the scope's read unit), on the
    optimized one each is its own short transaction."""
    item = await reads.one(get_tenant_item, id=item_id)
    other = await reads.one(get_tenant_item, id=_partner(item_id))
    if item is None or other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@s5_router.get("/s5list")
async def s5_list(
    user_id: UserId,
    tenant: Tenant,
    reads: PlannedReads,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """A declared ``ListOperation`` page (keyset; opaque, tamper-evident cursor).

    ``limit`` is checked by the planner (400 above the operation's cap), a
    tampered / foreign cursor is a 400."""
    page = await reads.page(list_tenant_items, limit=limit, cursor=cursor)
    return {"items": page.items, "next": page.next_cursor}


__all__ = ["s5_router"]
