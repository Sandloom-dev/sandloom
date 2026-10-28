"""S5 cost ATTRIBUTION stages — DIAGNOSTIC ONLY, NEVER A PRODUCTION CONFIGURATION.

Mounted only with ``BENCH_S5_DIAG=1`` (and ``BENCH_S5=1``). Every stage runs the
SAME handler body on the SAME execution path — ``DbSession`` request UoW,
``db.get(BenchTenantItem, id)``, ``ItemOut`` response model — and adds exactly
one security layer to the previous stage:

=====  ===================  ===================================================
stage  route / server env   adds
=====  ===================  ===================================================
S5-0   ``/d5/0/{id}``       plain PK read of the TenantScoped table inside
       owner DSN, no RLS    ``unscoped()`` (no auth, no tenant, no RLS)
S5-1   ``/d5/1/{id}``       + JWT parse/verify (``get_current_user_id``) — the
                            Redis revocation lookup is DISABLED (diag flag)
S5-2   ``/d5/2/{id}``       + tenant context: ``TenantScope()`` binding, the ORM
                            guard now filters (no ``unscoped()``)
S5-3   ``/d5/2/{id}``       + PostgreSQL RLS: ``tenant_rls`` engine, NON-superuser
       app role, RLS on     app role (tenant GUC set per transaction)
S5-4   ``/d5/2/{id}``       + the Redis revocation check (diag flag off)
S5-5   ``/d5/5/{id}``       + ``require(Subscribed(of=Account.user()))`` (claim-cheap)
S5-6   ``/s5/{id}``         the real secure route (control: should equal S5-5)
=====  ===================  ===================================================

``BENCH_DIAG_NO_REVOCATION=1`` (S5-0..S5-3) replaces the framework's
revocation hook with a no-op IN THIS PROCESS: tokens of revoked subjects are
ACCEPTED. It exists only to separate JWT verification from the revocation
round trip on a benchmark server and must never be set anywhere else.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from sandloom.core.di import DbSession, TenantScope, unscoped
from sandloom.shared.authz import require
from sandloom.shared.guards.auth import get_current_user_id

from ... import compat
from ..tenant_models import BenchTenantItem
from . import ItemOut

_log = logging.getLogger("benchsuite.diag_s5")

if os.environ.get("BENCH_DIAG_NO_REVOCATION") == "1":
    # DIAGNOSTIC ONLY: tokens are no longer checked against revocation marks.
    import sandloom.shared.authz.deps as _deps
    from sandloom.core.auth import verification

    async def _no_revocation_check(_request: Any, _claims: Any) -> None:
        return None

    verification.ensure_token_not_revoked = _no_revocation_check  # type: ignore[assignment]
    _deps.ensure_token_not_revoked = _no_revocation_check  # type: ignore[attr-defined]
    _log.warning("DIAGNOSTIC: token revocation checks are DISABLED in this process")

diag_router = APIRouter(tags=["bench-s5-diag (diagnostic only)"])
Tenant = Annotated[UUID, TenantScope()]
UserId = Annotated[UUID, Depends(get_current_user_id)]


def _out(item: Any) -> ItemOut:
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@diag_router.get(
    "/d5/0/{item_id}",
    response_model=ItemOut,
    dependencies=compat.public("bench: unauthenticated stage"),
)
async def d5_0(item_id: int, db: DbSession) -> ItemOut:
    with unscoped():
        return _out(await db.get(BenchTenantItem, item_id))


@diag_router.get("/d5/1/{item_id}", response_model=ItemOut)
async def d5_1(item_id: int, user_id: UserId, db: DbSession) -> ItemOut:
    with unscoped():
        return _out(await db.get(BenchTenantItem, item_id))


@diag_router.get("/d5/2/{item_id}", response_model=ItemOut)
async def d5_2(item_id: int, user_id: UserId, tenant: Tenant, db: DbSession) -> ItemOut:
    return _out(await db.get(BenchTenantItem, item_id))


@diag_router.get(
    "/d5/5/{item_id}",
    response_model=ItemOut,
    dependencies=[Depends(require(compat.subscribed()))],
)
async def d5_5(item_id: int, user_id: UserId, tenant: Tenant, db: DbSession) -> ItemOut:
    return _out(await db.get(BenchTenantItem, item_id))


__all__ = ["diag_router"]
