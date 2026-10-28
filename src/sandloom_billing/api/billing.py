"""Billing API (BILLING_SPEC §6). Base path: /api/v1/billing.

Two routers, one per operation class of ADR-0003 ("Operation classes"):

* :data:`billing_read_router` — the public plan catalog (class P) and
  **ordinary reads** (class R: the caller's own / organization's subscription
  status and credit balance). Declared reads (``PlannedReads``, read-only):
  no side effect at all — a tenant without a billing customer answers
  ``status="none"`` / balance 0 and no customer is created (checkout and the
  customer portal create it); nothing financial is decided here. Today these routes
  declare NO revocation staleness budget, so they are exactly as strict as
  before (staleness 0: the revocation and block marks are read live on every
  request). The class only makes a future, deliberate opt-in possible —
  ``revocation_freshness(...)`` / a contract-read budget on a single route, an
  owner decision recorded in ADR-0003 and pinned by
  ``tests/test_billing_route_classes.py``.
* :data:`billing_router` — **financial / billing management** (class F:
  checkout, customer portal). Always-strict (``strict_revocation()``): a
  staleness budget declared here is ignored. Each runs under an authorized,
  persisted operation intent (``services.operation_intents``, ADR-0003 G1):
  the intent is committed before the PSP call (whose idempotency key it
  provides), and the authority is re-verified after it — a demotion / removal
  that committed meanwhile gets ``403`` (another membership change ``409``, a
  revoked / blocked session ``401``); the intent is revoked, a checkout
  session expired at the PSP, and the URL never handed out. These routes take
  no request session: every write is its own short Unit of Work.

Both share the prefix, so paths, responses and the OpenAPI contract are
unchanged by the split.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from sandloom.sdk.authz import current_context
from sandloom.sdk.data import PlannedReads
from sandloom.sdk.infra import CoreRuntime, get_core
from sandloom.sdk.web import (
    DbSession,
    current_user_id,
    idempotency_guard,
    operation_intent,
    public,
)
from sandloom.shared.subject import UserContext
from sandloom_billing.psp import StripeClientPort, active_psp_client
from sandloom_billing.schemas import (
    CheckoutSessionIn,
    CreditsOut,
    CustomerPortalIn,
    PlansOut,
    SessionUrlOut,
    SubscriptionOut,
)
from sandloom_billing.services import BillingService
from sandloom_billing.services.operation_intents import OperationIntentService
from sandloom_billing.services.tenant_resolver import (
    BILLING_MANAGER_ROLE,
    resolve_tenant,
)

#: Public catalog + ordinary reads (ADR-0003 classes P / R). No budget
#: declared: strict (staleness 0) like every route without one; an opt-in is a
#: per-route, recorded decision.
billing_read_router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

#: Financial surface (ADR-0003 class F): always-strict revocation (a staleness
#: budget is ignored).
billing_router = APIRouter(
    prefix="/api/v1/billing",
    tags=["billing"],
)


async def get_psp() -> StripeClientPort:
    """Billing's module-private PSP client (overridable in tests).

    ``async`` although it does no I/O: a sync dependency would run in the
    thread pool on every request.

    Not a service-registry contract: the PSP client never leaves billing.
    """
    return active_psp_client()


@billing_read_router.get(
    "/plans",
    response_model=PlansOut,
    dependencies=[
        public(reason="public plan catalog (pricing page)"),
    ],
)
async def list_plans(db: DbSession) -> PlansOut:
    """Public catalog of active products and prices."""
    return await BillingService.list_plans(db)


@billing_read_router.get(
    "/subscription",
    response_model=SubscriptionOut,
)
async def get_subscription(
    reads: PlannedReads,
    org_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(current_user_id),
    subject: UserContext = Depends(current_context),
    core: CoreRuntime = Depends(get_core),
) -> SubscriptionOut:
    """Subscription status for the resolved tenant context."""
    # declared reads: a tenant without a billing customer gets
    # ``status="none"``; nothing is created
    tenant = await resolve_tenant(
        core=core, user_id=user_id, org_id=org_id, subject=subject
    )
    return await BillingService.tenant_subscription_status(
        tenant.tenant_type, tenant.tenant_id, reads
    )


@billing_read_router.get("/credits", response_model=CreditsOut)
async def get_credits(
    reads: PlannedReads,
    org_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(current_user_id),
    subject: UserContext = Depends(current_context),
    core: CoreRuntime = Depends(get_core),
) -> CreditsOut:
    """Credit balance for the resolved tenant context."""
    # a declared read: a tenant without a billing customer has 0; nothing is
    # created
    tenant = await resolve_tenant(
        core=core, user_id=user_id, org_id=org_id, subject=subject
    )
    balance = await BillingService.tenant_credit_balance(
        tenant.tenant_type, tenant.tenant_id, reads
    )
    return CreditsOut.of(balance)


#: Spec §4.6: sequential short units around the PSP call, the PSP call in
#: external_call(), release_check() before the URL leaves.
_INTENT = operation_intent(reason="PSP session issued under a persisted intent")


@billing_router.post(
    "/checkout-session", response_model=SessionUrlOut, dependencies=[_INTENT]
)
async def create_checkout_session(
    data: CheckoutSessionIn,
    request: Request,
    user_id: UUID = Depends(current_user_id),
    subject: UserContext = Depends(current_context),
    idempotency_key: str | None = Depends(
        idempotency_guard(
            scope="billing.checkout_intent", fail_closed=True, store_response=False
        )
    ),
    core: CoreRuntime = Depends(get_core),
    psp: StripeClientPort = Depends(get_psp),
) -> SessionUrlOut:
    """Start a checkout flow for the resolved tenant context."""
    ctx = await resolve_tenant(
        core=core,
        user_id=user_id,
        org_id=data.org_id,
        minimum_role=BILLING_MANAGER_ROLE,
        subject=subject,
        capture_authority=True,
    )
    url = await OperationIntentService.checkout(
        core=core,
        request=request,
        ctx=ctx,
        price_id=data.price_id,
        psp=psp,
        request_key=idempotency_key,
    )
    return SessionUrlOut(url=url)


@billing_router.post(
    "/customer-portal", response_model=SessionUrlOut, dependencies=[_INTENT]
)
async def create_customer_portal(
    data: CustomerPortalIn,
    request: Request,
    user_id: UUID = Depends(current_user_id),
    subject: UserContext = Depends(current_context),
    idempotency_key: str | None = Depends(
        idempotency_guard(
            scope="billing.customer_portal_intent",
            fail_closed=True,
            store_response=False,
        )
    ),
    core: CoreRuntime = Depends(get_core),
    psp: StripeClientPort = Depends(get_psp),
) -> SessionUrlOut:
    """Open the PSP customer portal for the resolved tenant context."""
    ctx = await resolve_tenant(
        core=core,
        user_id=user_id,
        org_id=data.org_id,
        minimum_role=BILLING_MANAGER_ROLE,
        subject=subject,
        capture_authority=True,
    )
    url = await OperationIntentService.customer_portal(
        core=core, request=request, ctx=ctx, psp=psp, request_key=idempotency_key
    )
    return SessionUrlOut(url=url)
