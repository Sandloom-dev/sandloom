"""Billing module: monetization (plans, subscriptions, credits, Stripe).

Monolith-first, microservice-ready (BILLING_SPEC.md). Registered via the core
plugin system; exposes:

- `billing.entitlements.v1`  — read contract for cross-domain guards
- `billing.credits.v1`       — command contract: spend a tenant's credits
- `billing.claims.v1`        — token-claims contributor (auto-registered)
- `billing.facts.v1`         — its access facts (``subscription.active``,
  ``organization.subscription.active``) for processes without billing
  (auto-registered with the facts)

Consumes (``ModuleDescriptor.consumes``): ``tenancy.membership.v1`` (optional).

Contract semantics: contract methods take no session; each call runs in
billing's own Unit of Work on billing's database (identically in-process and
over internal RPC). ``billing.credits.v1.spend`` is a command: idempotent on
its ``operation_key`` and NOT atomic with the caller's transaction (saga).
"""

from sandloom.sdk import define_module
from sandloom.sdk.infra import settings
from sandloom.sdk.module import ContractRequirement, ModuleAPI

from . import psp
from .access import ORG_SUBSCRIPTION_ACTIVE_FACT, SUBSCRIPTION_ACTIVE_FACT
from .claims import contribute_billing_claims
from .services.operations import OPERATIONS

ENTITLEMENTS_SERVICE_V1 = "billing.entitlements.v1"
CREDITS_SERVICE_V1 = "billing.credits.v1"
FACTS_SERVICE_V1 = "billing.facts.v1"
MEMBERSHIP_SERVICE_V1 = "tenancy.membership.v1"


def _setup(core: ModuleAPI) -> None:
    core.add_shutdown_task(psp.close_psp_client)

    # Production must never run on the fake PSP (fake checkouts; forged-event
    # risk). A CRITICAL startup task aborts the lifespan instead of serving.
    config_error = psp.psp_configuration_error(settings)
    if config_error is not None:
        core.logger.error("billing: %s", config_error)

        async def _refuse_fake_psp_in_prod() -> None:
            raise RuntimeError(config_error)

        core.add_startup_task(_refuse_fake_psp_in_prod, critical=True)

    # The PSP client is billing-INTERNAL (it can create checkout sessions and
    # verifies webhooks with billing's secrets): never a public service
    # contract, so no other module or RPC caller can reach it.
    psp.install_psp_client(psp.build_psp_client())

    # Subscription/plan claims baked into minted access tokens.
    core.add_claims_contributor(contribute_billing_claims)

    # Configurable access facts: Entitled("subscription.active") (personal
    # tenant; claim / live / auto per the ACCESS setting) and
    # Entitled("organization.subscription.active") (request's org; live).
    core.register_access_fact(SUBSCRIPTION_ACTIVE_FACT)
    core.register_access_fact(ORG_SUBSCRIPTION_ACTIVE_FACT)

    # Declared reads of GET /subscription and GET /credits (execution
    # planner; planned at startup, a schema error fails the boot).
    core.register_operations(*OPERATIONS)


# Discovered (sandloom.sdk define_module): the routers of ``api.__all__``, the
# producer classes of ``events`` and the ``@contract`` providers of
# ``contracts`` (billing.entitlements.v1, billing.credits.v1).
register = define_module(
    __name__,
    version="1.0.0",
    capabilities=["database", "eventbus"],
    consumes=[
        # Organization billing context. Optional: a personal-only deployment
        # (no tenancy anywhere) runs without it; when the token shows tenancy
        # IS deployed, billing answers 501 instead of silently using the
        # personal tenant (services/tenant_resolver).
        ContractRequirement(
            MEMBERSHIP_SERVICE_V1,
            required=False,
            reason="organization billing context / org subscription fact",
        ),
    ],
    setup=_setup,
)

__all__ = [
    "register",
    "ENTITLEMENTS_SERVICE_V1",
    "CREDITS_SERVICE_V1",
    "FACTS_SERVICE_V1",
]
