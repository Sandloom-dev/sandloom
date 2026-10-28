
"""Bench endpoints — the four scenarios, written the Sandloom way.

S2 uses Sandloom's real guard (``get_current_user_id``: JWT verify + framework-wide
revocation check), S3/S4 the request Unit of Work (``DbSession``), S4 the
transactional outbox via ``publish_event``. S5 is the realistic SaaS read:
JWT + ``TenantScope()`` + ``require(Subscribed(of=Account.user()))`` + a ``TenantScoped`` row
under PostgreSQL RLS (``api/s5.py``), mounted only with ``BENCH_S5=1``: its
``TenantScoped`` model switches the ORM tenant guard from its "no tenant
tables" shortcut to the per-statement verdict, which must not leak into the
S1-S4 numbers.
"""

import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from sandloom.core.di import DbSession
from sandloom.shared.guards.auth import get_current_user_id

from ... import compat
from .. import services


class ItemOut(BaseModel):
    id: int
    name: str
    value: int


class WriteIn(BaseModel):
    name: str
    value: int


class WriteOut(BaseModel):
    id: int


router = APIRouter(tags=["bench"])


@router.get("/s1", dependencies=compat.public("bench: no-op floor"))
async def s1() -> dict[str, bool]:
    return {"ok": True}


@router.get("/s2")
async def s2(user_id: UUID = Depends(get_current_user_id)) -> dict[str, object]:
    return {"ok": True, "user_id": str(user_id)}


@router.get(
    "/s3/{item_id}",
    response_model=ItemOut,
    dependencies=compat.public("bench: anonymous read"),
)
async def s3(item_id: int, db: DbSession) -> ItemOut:
    item = await services.get_item(db, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@router.post(
    "/s4",
    status_code=status.HTTP_201_CREATED,
    response_model=WriteOut,
    dependencies=compat.public("bench: anonymous write"),
)
async def s4(body: WriteIn, db: DbSession) -> WriteOut:
    return WriteOut(id=await services.create_write(db, body.name, body.value))


if os.environ.get("BENCH_S5", "").lower() in ("1", "true", "yes", "on"):
    from .s5 import s5_router

    router.include_router(s5_router)
    if os.environ.get("BENCH_S5_DIAG", "").lower() in ("1", "true", "yes", "on"):
        # DIAGNOSTIC ONLY (cost attribution stages) — never production.
        from .diag_s5 import diag_router

        router.include_router(diag_router)
    if os.environ.get("BENCH_WRITE_DIAG", "").lower() in ("1", "true", "yes", "on"):
        # DIAGNOSTIC ONLY (write-path stand, W1-W6) — never production.
        from .diag_write import diag_write_router

        router.include_router(diag_write_router)

__all__ = ["router"]
