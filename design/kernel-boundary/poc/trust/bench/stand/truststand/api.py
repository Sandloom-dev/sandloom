"""The request rung of T-15: a control, ``Role & Perm`` and a ``TenantScope``
route; the handlers do no work."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from sandloom.sdk import module_router, require
from sandloom.sdk.authz import Perm, Role, current_context
from sandloom.sdk.web import TenantScope

router = module_router()


@router.get("/ts/control")
async def control(_s: object = Depends(current_context)) -> dict[str, bool]:
    return {"ok": True}


@router.get(
    "/ts/role_and_perm",
    dependencies=[Depends(require(Role("admin") & Perm("perm.stand.read")))],
)
async def role_and_perm() -> dict[str, bool]:
    return {"ok": True}


@router.get("/ts/tenant")
async def tenant(
    tid: Annotated[UUID, TenantScope()], _s: object = Depends(current_context)
) -> dict[str, str]:
    return {"tenant": str(tid)}
