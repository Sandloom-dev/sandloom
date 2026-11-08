"""One authenticated GET per measured policy; the handler does no work.

``/azs/control`` is the same authenticated route without a policy, so a
policy's cost is its route minus the control.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from sandloom.sdk import module_router, require
from sandloom.sdk.authz import (
    Account,
    Credits,
    OrgRole,
    Perm,
    Role,
    Subscribed,
    SubscriptionActive,
    current_context,
)
from sandloom.sdk.web import TenantScope

router = module_router()

#: path -> policy (the order is the order of the report).
POLICIES = {
    "role": Role("admin"),
    "perm": Perm("perm.stand.read"),
    "subscribed": Subscribed(of=Account.user()),
    "subactive": SubscriptionActive(of=Account.user()),
    "credits": Credits(10, of=Account.user()),
    "role_or_credits": Role("admin") | Credits(10, of=Account.user()),
}


def _ok() -> dict[str, bool]:
    return {"ok": True}


@router.get("/azs/control")
async def control(_s: object = Depends(current_context)) -> dict[str, bool]:
    return _ok()


def _route(name: str) -> None:
    async def handler() -> dict[str, bool]:
        return _ok()

    handler.__name__ = f"azs_{name}"
    router.get(f"/azs/{name}", dependencies=[Depends(require(POLICIES[name]))])(handler)


for _name in POLICIES:
    _route(_name)

ORG_ADMIN = OrgRole("admin")
ORG_ADMIN_AND_SUB = OrgRole("admin") & SubscriptionActive(
    of=Account.org(param="org_id")
)
BOUND_ADMIN_AND_SUB = OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())


@router.get("/azs/orgrole/{org_id}", dependencies=[Depends(require(ORG_ADMIN))])
async def orgrole(org_id: UUID) -> dict[str, bool]:
    return _ok()


@router.get(
    "/azs/orgrole_and_subactive/{org_id}",
    dependencies=[Depends(require(ORG_ADMIN_AND_SUB))],
)
async def orgrole_and_subactive(org_id: UUID) -> dict[str, bool]:
    return _ok()


@router.get("/azs/bound/orgrole_and_subactive")
async def bound_orgrole_and_subactive(
    _tenant: Annotated[UUID, TenantScope()],
    _s: object = Depends(require(BOUND_ADMIN_AND_SUB)),
) -> dict[str, bool]:
    return _ok()


@router.get("/azs/bound/control")
async def bound_control(
    _tenant: Annotated[UUID, TenantScope()],
    _s: object = Depends(current_context),
) -> dict[str, bool]:
    return _ok()


# --- PoC kb-poc-authz additions (same atoms, kernel imports) ---------------- #
ORG_ADMIN_OR_SUB_USER = OrgRole("admin") | SubscriptionActive(of=Account.user())
NOT_MEMBER = ~OrgRole("member")


@router.get(
    "/azs/orgrole_or_subactive_user/{org_id}",
    dependencies=[Depends(require(ORG_ADMIN_OR_SUB_USER))],
)
async def orgrole_or_subactive_user(org_id: UUID) -> dict[str, bool]:
    return _ok()


@router.get("/azs/not_member/{org_id}", dependencies=[Depends(require(NOT_MEMBER))])
async def not_member(org_id: UUID) -> dict[str, bool]:
    return _ok()
