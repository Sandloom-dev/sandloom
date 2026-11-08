"""PA-3: ``OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())``
written over ``Entitled(<fact>, params=...)`` of the provider-side ``pa3``
module (``pa3.facts.v1.evaluate(name, subject, request, params)``).

The wrappers bind the organization / account in the consumer (what a
generated PA-3 wrapper with static binds would do) and send them as
parameters; the owner's resolver calls the domain contract in its process.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends

from sandloom.sdk import module_router, require
from sandloom.sdk.authz import TIER_LIVE, Account, Entitled, Requirement
from sandloom.sdk.web import TenantScope
from sandloom.shared.authz._resolve import resolve_org_id
from sandloom.shared.authz.account import resolve_account
from sandloom.shared.authz.results import AccessDecision

router = module_router()


class _Pa3(Requirement):
    tier = TIER_LIVE
    fact = ""

    def contracts(self) -> frozenset[str]:
        return frozenset({"pa3.facts.v1"})

    async def _params(self, ctx: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def decide(self, ctx: Any) -> AccessDecision:
        from sandloom.shared.authz.results import PolicyIndeterminate

        try:
            params = await self._params(ctx)
        except PolicyIndeterminate as exc:
            return exc.decision
        return await Entitled(self.fact, params=params, code=type(self).__name__).decide(
            ctx
        )

    async def evaluate(self, ctx: Any) -> bool:
        return await self._evaluate_via_decide(ctx)


class OrgRole(_Pa3):
    fact = "pa3.org_role"

    def __init__(self, minimum: str, *, org_param: str = "org_id") -> None:
        self.minimum = minimum
        self.org_param = org_param

    def org_params(self) -> frozenset[str]:
        return frozenset({self.org_param})

    async def _params(self, ctx: Any) -> dict[str, Any]:
        org = resolve_org_id(
            ctx,
            org_param=self.org_param,
            requirement=self.describe(),
            validate_against_token=False,
        )
        return {
            "organization_id": str(org),
            "user_id": str(ctx.subject.user_id),
            "minimum": self.minimum,
        }


class SubscriptionActive(_Pa3):
    fact = "pa3.subscription_active"

    def __init__(self, *, of: Account) -> None:
        self.account = of

    def org_params(self) -> frozenset[str]:
        return frozenset({"<bound tenant>"})

    async def _params(self, ctx: Any) -> dict[str, Any]:
        kind, tenant = await resolve_account(
            ctx, self.account, requirement=self.describe()
        )
        return {"tenant_type": kind, "tenant_id": str(tenant)}


BOUND_ADMIN_AND_SUB = OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())


@router.get("/azs/bound/orgrole_and_subactive")
async def bound_orgrole_and_subactive(
    _tenant: Annotated[UUID, TenantScope()],
    _s: object = Depends(require(BOUND_ADMIN_AND_SUB)),
) -> dict[str, bool]:
    return {"ok": True}
