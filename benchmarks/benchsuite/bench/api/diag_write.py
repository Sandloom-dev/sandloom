"""Write-path stand routes — DIAGNOSTIC ONLY, NEVER A PRODUCTION CONFIGURATION.

Mounted only with ``BENCH_WRITE_DIAG=1`` (and ``BENCH_S5=1``). Every route
carries the secure S5 stack — ``get_current_user_id`` (JWT + revocation
check), ``TenantScope()``, ``require(Subscribed(of=Account.user()))`` and a ``TenantScoped``
table under FORCEd RLS — so the numbers compare with S5. The rungs and what
each adds: :mod:`benchsuite.bench.write_diag`.

``?fail=true`` raises after all of the route's writes (409): the atomicity
tests prove the outbox / audit rows and the on_commit callback commit iff the
change commits.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from sandloom.core.di import (
    DbSession,
    TenantScope,
    get_core,
    idempotency_guard,
)
from sandloom.shared.authz import require
from sandloom.shared.guards.auth import get_current_user_id

from ... import compat
from .. import write_diag
from . import WriteIn, WriteOut

diag_write_router = APIRouter(
    tags=["bench-write-diag (diagnostic only)"],
    dependencies=[Depends(require(compat.subscribed()))],
)
Tenant = Annotated[UUID, TenantScope()]
UserId = Annotated[UUID, Depends(get_current_user_id)]
Created = {"status_code": status.HTTP_201_CREATED, "response_model": WriteOut}


class ValueOut(BaseModel):
    value: int


class CommandIn(BaseModel):
    name: str
    value: int
    key: str


@diag_write_router.post("/dw/1", **Created)
async def w1(
    body: WriteIn, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> WriteOut:
    return WriteOut(id=await write_diag.w_insert(db, body.name, body.value, fail=fail))


@diag_write_router.post("/dw/2", **Created)
async def w2(
    body: WriteIn, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> WriteOut:
    new_id = await write_diag.w_insert(db, body.name, body.value, event=True, fail=fail)
    return WriteOut(id=new_id)


@diag_write_router.post("/dw/3", **Created)
async def w3(
    body: WriteIn, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> WriteOut:
    new_id = await write_diag.w_insert(
        db, body.name, body.value, event=True, audit_actor=user_id, fail=fail
    )
    return WriteOut(id=new_id)


@diag_write_router.post(
    "/dw/7",
    dependencies=compat.legacy_marker("strict_revocation"),
    **Created,
)
async def w7(
    body: WriteIn,
    user_id: UserId,
    tenant: Tenant,
    db: DbSession,
    fail: bool = False,
    # After authentication (create_app refuses a guard solved before it).
    _key: str | None = Depends(idempotency_guard(scope="bench.dw7", durable=True)),
) -> WriteOut:
    """W3 as an ADR-0003 class-F write: always-strict revocation and a
    durable ``idempotency_guard`` (the request needs ``Idempotency-Key``; its
    record commits with the write — ``create_app`` refuses a cache guard on
    a class-F route)."""
    new_id = await write_diag.w_insert(
        db, body.name, body.value, event=True, audit_actor=user_id, fail=fail
    )
    return WriteOut(id=new_id)


@diag_write_router.post("/dw/4o/{counter_id}", response_model=ValueOut)
async def w4o(
    counter_id: int, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> ValueOut:
    return ValueOut(value=await write_diag.w4_orm(db, counter_id, fail=fail))


@diag_write_router.post("/dw/4c/{counter_id}", response_model=ValueOut)
async def w4c(
    counter_id: int, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> ValueOut:
    return ValueOut(value=await write_diag.w4_core(db, counter_id, fail=fail))


@diag_write_router.post("/dw/m/{counter_id}", response_model=ValueOut)
async def wm(
    counter_id: int, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> ValueOut:
    """WM: load 20 mapped rows and assign 10 attributes on each (200 mapped
    assignments): the price of per-assignment checks (spec §10.4)."""
    return ValueOut(value=await write_diag.wm_orm(db, counter_id, fail=fail))


@diag_write_router.post("/dw/5", **Created)
async def w5(
    body: WriteIn, user_id: UserId, tenant: Tenant, db: DbSession, fail: bool = False
) -> WriteOut:
    order_id = await write_diag.place_order(
        db, body.name, body.value, user_id, fail=fail
    )
    return WriteOut(id=order_id)


@diag_write_router.post("/dw/6", **Created)
async def w6(
    body: CommandIn,
    user_id: UserId,
    tenant: Tenant,
    core: Any = Depends(get_core),
    fail: bool = False,
) -> WriteOut:
    """W5 as the provider's command: no request UoW; the contract opens its own."""
    contract = core.get_service(write_diag.SERVICE)
    method = contract.place_order_failing if fail else contract.place_order
    order_id = await method(body.name, body.value, user_id, operation_key=body.key)
    return WriteOut(id=order_id)


__all__ = ["diag_write_router"]
