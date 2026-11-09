"""Routes gated by kernel atoms whose data tenancy, billing and acme_idp supply.

- ``/subscribed`` — billing's ``subscription_status`` claim (claim tier, 402);
- ``/orgs/{org_id}/admin`` — ``OrgRole("admin")``, live over
  ``tenancy.membership.v1``;
- ``/orgs/{org_id}/subscribed`` — ``SubscriptionActive(of=Account.org())``,
  live over billing;
- ``/tenant`` — ``TenantScope``: the ``X-Tenant-Id`` header admitted only
  when tenancy's ``tenant_ids`` claim grants it;
- ``/platform`` — ``require_platform_admin`` (acme_idp's ``roles``);
- ``/me`` — the subject the kernel formed from the token.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends

from sandloom.sdk import CurrentUser, module_router
from sandloom.sdk.authz import (
    Account,
    OrgRole,
    Perm,
    Subscribed,
    SubscriptionActive,
    current_context,
    require,
)
from sandloom.sdk.web import TenantScope, require_platform_admin
from sandloom.shared.subject import UserContext

router = module_router()


@router.get("/me")
async def me(subject: UserContext = Depends(current_context)) -> dict[str, Any]:
    return {
        "user_id": str(subject.user_id),
        "roles": sorted(subject.roles),
        "permissions": sorted(subject.permissions),
    }


@router.get(
    "/subscribed", dependencies=[Depends(require(Subscribed(of=Account.user())))]
)
async def subscribed(user: CurrentUser) -> dict[str, bool]:
    return {"ok": True}


@router.get("/orgs/{org_id}/admin", dependencies=[Depends(require(OrgRole("admin")))])
async def org_admin(org_id: UUID) -> dict[str, str]:
    return {"org": str(org_id)}


@router.get(
    "/orgs/{org_id}/subscribed",
    dependencies=[Depends(require(SubscriptionActive(of=Account.org())))],
)
async def org_subscribed(org_id: UUID) -> dict[str, str]:
    return {"org": str(org_id)}


@router.get("/tenant")
async def tenant(
    user: CurrentUser, tid: Annotated[UUID, TenantScope()]
) -> dict[str, str]:
    return {"tenant": str(tid)}


@router.get("/platform", dependencies=[Depends(require_platform_admin)])
async def platform(user: CurrentUser) -> dict[str, str]:
    return {"ok": "platform admin"}


@router.get("/manage", dependencies=[Depends(require(Perm("acme.platform.manage")))])
async def manage(user: CurrentUser) -> dict[str, str]:
    return {"ok": "acme.platform.manage"}
