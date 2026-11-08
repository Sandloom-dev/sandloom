"""PoC kb-poc-authz, PA-3 provider side: two parameterized access facts.

Loaded in the provider process next to tenancy and billing. Each fact's live
resolver reads its parameters from the evaluation's resource (the patched
``facts.v1.evaluate(..., params)``) and calls the domain contract locally,
which is what an owner-registered parameterized ``AccessFact`` would do.
"""

from typing import Any
from uuid import UUID

from sandloom.sdk import define_module
from sandloom.sdk.authz import AccessFact, Category


async def _org_role(service: Any, ctx: Any) -> bool:
    p = await ctx.get_resource()
    return bool(
        await service.has_role_at_least(
            UUID(p["organization_id"]), UUID(p["user_id"]), p["minimum"]
        )
    )


async def _subscription_active(service: Any, ctx: Any) -> bool:
    p = await ctx.get_resource()
    return bool(
        await service.check_subscription_active(p["tenant_type"], UUID(p["tenant_id"]))
    )


FACTS = (
    AccessFact(
        name="pa3.org_role",
        owner_module="pa3",
        live_service="tenancy.membership.v1",
        live_resolver=_org_role,
        min_freshness="live",
        default_mode="live",
    ),
    AccessFact(
        name="pa3.subscription_active",
        owner_module="pa3",
        live_service="billing.entitlements.v1",
        live_resolver=_subscription_active,
        min_freshness="live",
        default_mode="live",
        deny_category=Category.PAYMENT_REQUIRED,
    ),
)


def _setup(core: Any) -> None:
    for fact in FACTS:
        core.register_access_fact(fact)


register = define_module(__name__, version="0.1.0", setup=_setup)
