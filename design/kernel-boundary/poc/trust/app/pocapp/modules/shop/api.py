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
    dependencies=[Depends(require(Entitled("inventory.stock.available")))],
)
async def buy(sku: str, user: CurrentUser) -> dict[str, str]:
    return {"bought": sku}


# ---- kb-poc-trust: algebra over the trusted view (T-9, T-13) --------------
from sandloom.sdk.authz import Requirement  # noqa: E402


class AlwaysAllow(Requirement):
    """An allowing branch that needs no protected claim (T-9 ``Or``)."""

    async def evaluate(self, ctx: object) -> bool:
        return True

    def describe(self) -> str:
        return "always"


@router.get("/plain")
async def plain(user: CurrentUser) -> dict[str, str]:
    return {"ok": "authenticated"}


@router.get(
    "/role-or-always", dependencies=[Depends(require(Role("admin") | AlwaysAllow()))]
)
async def role_or_always(user: CurrentUser) -> dict[str, str]:
    return {"ok": "Role(admin) | always"}


@router.get("/not-role", dependencies=[Depends(require(~Role("admin")))])
async def not_role(user: CurrentUser) -> dict[str, str]:
    return {"ok": "~Role(admin)"}


@router.get(
    "/role-or-missing",
    dependencies=[Depends(require(Role("admin") | Entitled("missing.fact")))],
)
async def role_or_missing(user: CurrentUser) -> dict[str, str]:
    return {"ok": "Role(admin) | Entitled(missing.fact)"}


@router.get("/not-missing", dependencies=[Depends(require(~Entitled("missing.fact")))])
async def not_missing(user: CurrentUser) -> dict[str, str]:
    return {"ok": "~Entitled(missing.fact)"}


@router.get(
    "/role-and-perm",
    dependencies=[Depends(require(Role("admin") & Perm("perm.poc.write")))],
)
async def role_and_perm(user: CurrentUser) -> dict[str, str]:
    return {"ok": "Role(admin) & Perm"}
