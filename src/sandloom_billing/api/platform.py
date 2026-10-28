"""Strict platform-owner billing API. No organization membership impersonation."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sandloom.sdk.infra import settings
from sandloom.sdk.web import (
    DbSession,
    at_commit,
    current_user_id,
    require_platform_admin,
)
from sandloom_billing.schemas.platform import (
    CreditAdjustment,
    CreditAdjustmentResult,
    PlatformBalance,
    PlatformCustomer,
    PlatformGrant,
    PlatformIntent,
    PlatformLedger,
    PlatformPaymentState,
    PlatformPrice,
    PlatformProduct,
    PlatformSubscription,
    PlatformWebhook,
    ProductUpdate,
    ProviderInfo,
)
from sandloom_billing.services.platform_service import PlatformBillingService

platform_billing_router = APIRouter(
    prefix="/api/v1/platform/billing",
    tags=["platform-billing"],
    dependencies=[Depends(require_platform_admin)],
)


@platform_billing_router.get(
    "/customers",
    response_model=list[PlatformCustomer],
)
async def list_customers(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    user_id: UUID | None = None,
    organization_id: UUID | None = None,
) -> list[PlatformCustomer]:
    rows = await PlatformBillingService.list_customer(
        db, skip, limit, user_id, organization_id
    )
    return [PlatformCustomer.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/customers/{record_id}",
    response_model=PlatformCustomer,
)
async def get_customers(record_id: UUID, db: DbSession) -> PlatformCustomer:
    return PlatformCustomer.model_validate(
        await PlatformBillingService.get_customer(record_id, db)
    )


@platform_billing_router.get(
    "/subscriptions",
    response_model=list[PlatformSubscription],
)
async def list_subscriptions(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    customer_id: UUID | None = None,
    status: str | None = None,
) -> list[PlatformSubscription]:
    rows = await PlatformBillingService.list_subscription(
        db, skip, limit, customer_id, status
    )
    return [PlatformSubscription.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/subscriptions/{record_id}",
    response_model=PlatformSubscription,
)
async def get_subscriptions(record_id: int, db: DbSession) -> PlatformSubscription:
    return PlatformSubscription.model_validate(
        await PlatformBillingService.get_subscription(record_id, db)
    )


@platform_billing_router.get(
    "/products",
    response_model=list[PlatformProduct],
)
async def list_products(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    is_active: bool | None = None,
) -> list[PlatformProduct]:
    rows = await PlatformBillingService.list_product(db, skip, limit, is_active)
    return [PlatformProduct.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/products/{record_id}",
    response_model=PlatformProduct,
)
async def get_products(record_id: int, db: DbSession) -> PlatformProduct:
    return PlatformProduct.model_validate(
        await PlatformBillingService.get_product(record_id, db)
    )


@platform_billing_router.get(
    "/prices",
    response_model=list[PlatformPrice],
)
async def list_prices(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    product_id: int | None = None,
) -> list[PlatformPrice]:
    rows = await PlatformBillingService.list_price(db, skip, limit, product_id)
    return [PlatformPrice.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/prices/{record_id}",
    response_model=PlatformPrice,
)
async def get_prices(record_id: int, db: DbSession) -> PlatformPrice:
    return PlatformPrice.model_validate(
        await PlatformBillingService.get_price(record_id, db)
    )


@platform_billing_router.get(
    "/balances",
    response_model=list[PlatformBalance],
)
async def list_balances(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    customer_id: UUID | None = None,
) -> list[PlatformBalance]:
    rows = await PlatformBillingService.list_balance(db, skip, limit, customer_id)
    return [PlatformBalance.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/balances/{record_id}",
    response_model=PlatformBalance,
)
async def get_balances(record_id: int, db: DbSession) -> PlatformBalance:
    return PlatformBalance.model_validate(
        await PlatformBillingService.get_balance(record_id, db)
    )


@platform_billing_router.get(
    "/ledger",
    response_model=list[PlatformLedger],
)
async def list_ledger(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    customer_id: UUID | None = None,
) -> list[PlatformLedger]:
    rows = await PlatformBillingService.list_ledger(db, skip, limit, customer_id)
    return [PlatformLedger.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/ledger/{record_id}",
    response_model=PlatformLedger,
)
async def get_ledger(record_id: int, db: DbSession) -> PlatformLedger:
    return PlatformLedger.model_validate(
        await PlatformBillingService.get_ledger(record_id, db)
    )


@platform_billing_router.get(
    "/grants",
    response_model=list[PlatformGrant],
)
async def list_grants(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    customer_id: UUID | None = None,
) -> list[PlatformGrant]:
    rows = await PlatformBillingService.list_grant(db, skip, limit, customer_id)
    return [PlatformGrant.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/grants/{record_id}",
    response_model=PlatformGrant,
)
async def get_grants(record_id: int, db: DbSession) -> PlatformGrant:
    return PlatformGrant.model_validate(
        await PlatformBillingService.get_grant(record_id, db)
    )


@platform_billing_router.get(
    "/webhooks",
    response_model=list[PlatformWebhook],
)
async def list_webhooks(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    status: str | None = None,
) -> list[PlatformWebhook]:
    rows = await PlatformBillingService.list_webhook(db, skip, limit, status)
    return [PlatformWebhook.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/webhooks/{record_id}",
    response_model=PlatformWebhook,
)
async def get_webhooks(record_id: int, db: DbSession) -> PlatformWebhook:
    return PlatformWebhook.model_validate(
        await PlatformBillingService.get_webhook(record_id, db)
    )


@platform_billing_router.get(
    "/intents",
    response_model=list[PlatformIntent],
)
async def list_intents(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    customer_id: UUID | None = None,
    status: str | None = None,
    followup: bool | None = None,
) -> list[PlatformIntent]:
    rows = await PlatformBillingService.list_intent(
        db, skip, limit, customer_id, status, followup
    )
    return [PlatformIntent.model_validate(row) for row in rows]


@platform_billing_router.get(
    "/intents/{record_id}",
    response_model=PlatformIntent,
)
async def get_intents(record_id: UUID, db: DbSession) -> PlatformIntent:
    return PlatformIntent.model_validate(
        await PlatformBillingService.get_intent(record_id, db)
    )


@platform_billing_router.post(
    "/customers/{record_id}/adjust-credits",
    response_model=CreditAdjustmentResult,
    dependencies=[at_commit()],
)
async def adjust_credits(
    record_id: UUID,
    data: CreditAdjustment,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> CreditAdjustmentResult:
    return await PlatformBillingService.adjust(record_id, data, actor_id, db)


@platform_billing_router.patch(
    "/products/{record_id}",
    response_model=PlatformProduct,
    dependencies=[at_commit()],
)
async def update_product(
    record_id: int,
    data: ProductUpdate,
    db: DbSession,
    actor_id: UUID = Depends(current_user_id),
) -> PlatformProduct:
    return PlatformProduct.model_validate(
        await PlatformBillingService.update_product(record_id, data, actor_id, db)
    )


@platform_billing_router.get(
    "/payment-states",
    response_model=list[PlatformPaymentState],
)
async def payment_states(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    psp_customer_id: str | None = Query(None, max_length=255),
) -> list[PlatformPaymentState]:
    rows = await PlatformBillingService.payment_states(db, skip, limit, psp_customer_id)
    return [
        PlatformPaymentState(
            id=row.psp_payment_intent_id,
            psp_customer_id=row.psp_customer_id,
            currency=row.currency,
            amount_refunded=row.amount_refunded,
            dispute_status=row.dispute_status,
            dispute_amount=row.dispute_amount,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@platform_billing_router.get(
    "/provider",
    response_model=ProviderInfo,
)
async def provider_info() -> ProviderInfo:
    cfg = settings
    if cfg.billing_provider == "fake":
        return ProviderInfo(provider="fake", mode="demo", dashboard_url=None)
    live = bool(
        cfg.stripe_secret_key
        and cfg.stripe_secret_key.startswith(("sk_live_", "rk_live_"))
    )
    return ProviderInfo(
        provider="stripe",
        mode="live" if live else "test",
        dashboard_url="https://dashboard.stripe.com"
        if live
        else "https://dashboard.stripe.com/test",
    )
