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
from sandloom.sdk.module import CapabilityRequirement, ContractRequirement, ModuleAPI

from . import psp
from .access import ORG_SUBSCRIPTION_ACTIVE_FACT, SUBSCRIPTION_ACTIVE_FACT
from .claims import contribute_billing_claims
from .config import BillingSettings
from .services.operations import OPERATIONS
from .services.reconciler_health import RECONCILER_HEALTH_CHECK, ReconcilerHealthCheck

ENTITLEMENTS_SERVICE_V1 = "billing.entitlements.v1"
CREDITS_SERVICE_V1 = "billing.credits.v1"
FACTS_SERVICE_V1 = "billing.facts.v1"
MEMBERSHIP_SERVICE_V1 = "tenancy.membership.v1"

#: The claim keys billing writes, declared fail-safe (ADR-0014 decision 7,
#: billing option (c)): every reader escalates or answers indeterminate when
#: one is absent (the generated ``Subscribed`` escalates to the live fact or
#: answers 503 when billing failed at the mint, ``Feature`` / ``WithinLimit``
#: answer 501 / 503, ``plan_code`` grants nothing), so billing's contributor
#: may be non-critical and a billing outage still mints a login. An issuer
#: that binds billing remotely ``"critical": false`` reads the same
#: declaration from its composition root
#: (``Authority("sandloom_billing", fail_safe=True)``).
BILLING_CLAIM_KEYS: tuple[str, ...] = (
    "subscription_status",
    "plan_code",
    "features",
    "limits",
)


def _setup(core: ModuleAPI) -> None:
    """Register billing's PSP client, claims, access facts and reads.

    The PSP configuration (the fake PSP in prod without the accepted risk
    ``billing.fake_psp_in_prod``, Stripe without its keys) is refused earlier,
    by ``BillingSettings``: the loader validates the model before this setup
    runs, so every process that builds the composition (the app, worker, beat,
    ``sandloom migrate``, the operator CLIs) stops with the kernel's
    INVALID_CONFIGURATION refusal before the PSP client is built.
    """
    core.add_shutdown_task(psp.close_psp_client)

    # The PSP client is billing-INTERNAL (it can create checkout sessions and
    # verifies webhooks with billing's secrets): never a public service
    # contract, so no other module or RPC caller can reach it.
    psp.install_psp_client(psp.build_psp_client())

    # Subscription/plan claims baked into minted access tokens.
    core.add_claims_contributor(contribute_billing_claims, fail_safe=BILLING_CLAIM_KEYS)

    # Configurable access facts, owned by billing:
    # Entitled("subscription.active", owner="billing") (personal tenant;
    # claim / live / auto per the ACCESS setting) and
    # Entitled("organization.subscription.active", owner="billing")
    # (request's org; live).
    core.register_access_fact(SUBSCRIPTION_ACTIVE_FACT)
    core.register_access_fact(ORG_SUBSCRIPTION_ACTIVE_FACT)

    # Declared reads of GET /subscription and GET /credits (execution
    # planner; planned at startup, a schema error fails the boot).
    core.register_operations(*OPERATIONS)

    core.register_health_check(
        RECONCILER_HEALTH_CHECK, ReconcilerHealthCheck(), operational=True
    )


# Discovered (sandloom.sdk define_module): the routers of ``api.__all__``, the
# producer classes of ``events`` and the ``@contract`` providers of
# ``contracts`` (billing.entitlements.v1, billing.credits.v1).
register = define_module(
    __name__,
    version="1.0.0",
    capabilities=[
        "database",
        "eventbus",
        # The intent reconciler (tasks.py) closes and escalates payment
        # operations no request will close: it must run at its interval, so
        # the task queue's driver must run periodic jobs, not only enqueue.
        CapabilityRequirement("taskqueue", features=frozenset({"periodic"})),
    ],
    consumes=[
        # Organization billing context. Optional: a personal-only deployment
        # (no tenancy anywhere) runs without it; when the token shows tenancy
        # IS deployed, billing answers 501 instead of silently using the
        # personal tenant (services/tenant_resolver).
        ContractRequirement(
            MEMBERSHIP_SERVICE_V1,
            required=False,
            reason="organization billing context / org subscription fact",
            # Called through core.get_service (services/tenant_resolver,
            # access): stated, so the generated expectation holds tenancy to
            # these methods only (ADR-0013 decision 2).
            methods=("get_member_state", "get_role", "has_role_at_least"),
        ),
    ],
    setup=_setup,
    settings=BillingSettings,
)

__all__ = [
    "register",
    "ENTITLEMENTS_SERVICE_V1",
    "CREDITS_SERVICE_V1",
    "FACTS_SERVICE_V1",
]
