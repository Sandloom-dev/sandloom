"""Entitlements: billing-internal reads + the ``billing.entitlements.v1`` contract.

- :class:`EntitlementsService` — billing-INTERNAL helpers that take the
  caller's (billing's own) session; used by billing's own services/claims.
- :class:`sandloom_billing.contracts.EntitlementsContract` — the object registered as
  ``billing.entitlements.v1``. Other modules resolve it through
  ``core.get_service`` — no billing imports required (spec §11). Its methods
  take NO session: every call runs in billing's OWN read-only Unit of Work on
  the database the billing module declares, identically in a monolith and
  over internal RPC (see :func:`sandloom.core.module_api.contract_method`).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingCustomer
from sandloom_billing.services.billing_service import BillingService

ENTITLEMENTS_SERVICE_V1 = "billing.entitlements.v1"


class EntitlementsService:
    """Billing-internal read helpers (take billing's own session)."""

    @classmethod
    async def find_customer(
        cls,
        tenant_type: str,
        tenant_id: UUID,
        db: AsyncSession,
    ) -> BillingCustomer | None:
        query = select(BillingCustomer)
        if tenant_type == "user":
            query = query.where(BillingCustomer.user_id == tenant_id)
        elif tenant_type == "organization":
            query = query.where(BillingCustomer.organization_id == tenant_id)
        else:
            raise ValueError(f"unknown billing tenant_type {tenant_type!r}")
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @classmethod
    async def check_subscription_active(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> bool:
        subs = await BillingService.tenant_subscriptions(tenant_type, tenant_id, db)
        if not subs:
            return False
        _, grants = BillingService._current_subscription(subs)
        return grants

    @classmethod
    async def get_credit_balance(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> int:
        customer = await cls.find_customer(tenant_type, tenant_id, db)
        if customer is None:
            return 0
        return await BillingService.get_credit_balance(customer.id, db)

    @classmethod
    async def subscription_claims(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> dict[str, object]:
        """Claim snapshot for token minting: subscription status + plan code.

        Returns ``{}`` when the tenant has no billing customer yet, so the
        access token simply carries no subscription claims.
        """
        # Customer + subscriptions + their plans in ONE read (was three).
        loaded = await BillingService.tenant_subscriptions_with_plans(
            tenant_type, tenant_id, db
        )
        if loaded is None:
            return {}
        subs, plan_names = loaded
        # Same predicate as check_subscription_active (period + grace policy),
        # so claim-mode and live-mode gates agree at mint time.
        return await BillingService.claims_for_subscriptions(
            subs, db, plan_names=plan_names
        )
