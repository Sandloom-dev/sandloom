"""Routes gated by kernel atoms whose data the test batteries supply."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from sandloom.sdk import CurrentUser, module_router
from sandloom.sdk.authz import Entitled, Perm, Role, require
from sandloom.sdk.web import TenantScope, require_platform_admin

router = module_router()


@router.get("/role-admin", dependencies=[Depends(require(Role("admin")))])
async def role_admin(user: CurrentUser) -> dict[str, str]:
    return {"ok": "role admin"}


@router.get("/perm-write", dependencies=[Depends(require(Perm("perm.poc.write")))])
async def perm_write(user: CurrentUser) -> dict[str, str]:
    return {"ok": "perm.poc.write"}


@router.get("/platform", dependencies=[Depends(require_platform_admin)])
async def platform(user: CurrentUser) -> dict[str, str]:
    return {"ok": "platform admin"}


@router.get("/tenant")
async def tenant(
    user: CurrentUser, tid: Annotated[UUID, TenantScope()]
) -> dict[str, str]:
    return {"tenant": str(tid)}


@router.get(
    "/items/{sku}/buy",
    dependencies=[
        Depends(require(Entitled("inventory.stock.available", owner="inventory")))
    ],
)
async def buy(sku: str, user: CurrentUser) -> dict[str, str]:
    return {"bought": sku}
