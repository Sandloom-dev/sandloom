"""Platform billing operations, confined to billing's own persistence."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom_billing.models.catalog import BillingPrice, BillingProduct
from sandloom_billing.models.credits import (
    CreditBalance,
    CreditGrant,
    CreditLedgerEntry,
    PspPaymentState,
)
from sandloom_billing.models.customer import BillingCustomer
from sandloom_billing.models.intent import OperationIntent
from sandloom_billing.models.stripe_event import StripeEvent
from sandloom_billing.models.subscription import BillingSubscription
from sandloom_billing.schemas.platform import (
    CreditAdjustment,
    CreditAdjustmentResult,
    ProductUpdate,
)
from sandloom_billing.services.billing_service import BillingService


class PlatformBillingService:
    @staticmethod
    async def adjust(
        customer_id: UUID, data: CreditAdjustment, actor_id: UUID, db: AsyncSession
    ) -> CreditAdjustmentResult:
        # Serialize operator adjustments before checking payload-bound dedupe.
        # NO KEY UPDATE serializes adjustments but allows the KEY SHARE locks
        # taken by ordinary ledger/balance FK checks. FOR UPDATE could invert
        # the customer/balance lock order against a concurrent ordinary spend.
        # The unique ledger key and the effect commit in this same UoW.
        customer = await db.scalar(
            select(BillingCustomer)
            .where(BillingCustomer.id == customer_id)
            .with_for_update(key_share=True)
        )
        if customer is None:
            raise HTTPException(404, "Billing customer not found")
        key = f"platform-credit:{data.operation_key}"
        delta = data.amount if data.direction == "grant" else -data.amount
        previous = await db.scalar(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.customer_id == customer_id,
                CreditLedgerEntry.operation_key == key,
            )
        )
        if previous is not None:
            if previous.delta != delta or previous.reason != data.reason:
                raise HTTPException(
                    409,
                    "Operation reference was already used with different parameters",
                )
            if previous.balance_after is None:
                raise HTTPException(409, "Original adjustment result is unavailable")
            return CreditAdjustmentResult(
                id=customer_id,
                balance_after=previous.balance_after,
                operation_key=data.operation_key,
                replayed=True,
            )
        apply = (
            BillingService.add_credits
            if data.direction == "grant"
            else BillingService.spend_credits
        )
        balance = await apply(customer, data.amount, key, db, reason=data.reason)
        record_audit(
            "billing.platform.credit_adjusted",
            actor_id=actor_id,
            resource_type="billing_customer",
            resource_id=str(customer_id),
            data={
                "delta": delta,
                "reason": data.reason,
                "operation_key": str(data.operation_key),
            },
        )
        return CreditAdjustmentResult(
            id=customer_id,
            balance_after=balance,
            operation_key=data.operation_key,
            replayed=False,
        )

    @staticmethod
    async def update_product(
        record_id: int, data: ProductUpdate, actor_id: UUID, db: AsyncSession
    ) -> BillingProduct:
        product = await db.scalar(
            select(BillingProduct)
            .where(BillingProduct.id == record_id)
            .with_for_update()
        )
        if product is None:
            raise HTTPException(404, "Product not found")
        before = {"name": product.name, "is_active": product.is_active}
        product.name, product.is_active = data.name, data.is_active
        record_audit(
            "billing.platform.product_updated",
            actor_id=actor_id,
            resource_type="billing_product",
            resource_id=str(record_id),
            data={
                "before": before,
                "after": {"name": data.name, "is_active": data.is_active},
                "reason": data.reason,
            },
        )
        await db.flush()
        return product

    @staticmethod
    async def list_customer(
        db: AsyncSession,
        skip: int,
        limit: int,
        user_id: UUID | None,
        organization_id: UUID | None,
    ) -> list[BillingCustomer]:
        query = select(BillingCustomer)
        if user_id is not None:
            query = query.where(BillingCustomer.user_id == user_id)
        if organization_id is not None:
            query = query.where(BillingCustomer.organization_id == organization_id)
        return list(
            (
                await db.scalars(
                    query.order_by(BillingCustomer.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_customer(record_id: UUID, db: AsyncSession) -> BillingCustomer:
        row = await db.get(BillingCustomer, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_subscription(
        db: AsyncSession,
        skip: int,
        limit: int,
        customer_id: UUID | None,
        status: str | None,
    ) -> list[BillingSubscription]:
        query = select(BillingSubscription)
        if customer_id is not None:
            query = query.where(BillingSubscription.customer_id == customer_id)
        if status is not None:
            query = query.where(BillingSubscription.status == status)
        return list(
            (
                await db.scalars(
                    query.order_by(BillingSubscription.id.desc())
                    .offset(skip)
                    .limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_subscription(record_id: int, db: AsyncSession) -> BillingSubscription:
        row = await db.get(BillingSubscription, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_product(
        db: AsyncSession, skip: int, limit: int, is_active: bool | None
    ) -> list[BillingProduct]:
        query = select(BillingProduct)
        if is_active is not None:
            query = query.where(BillingProduct.is_active == is_active)
        return list(
            (
                await db.scalars(
                    query.order_by(BillingProduct.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_product(record_id: int, db: AsyncSession) -> BillingProduct:
        row = await db.get(BillingProduct, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_price(
        db: AsyncSession, skip: int, limit: int, product_id: int | None
    ) -> list[BillingPrice]:
        query = select(BillingPrice)
        if product_id is not None:
            query = query.where(BillingPrice.product_id == product_id)
        return list(
            (
                await db.scalars(
                    query.order_by(BillingPrice.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_price(record_id: int, db: AsyncSession) -> BillingPrice:
        row = await db.get(BillingPrice, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_balance(
        db: AsyncSession, skip: int, limit: int, customer_id: UUID | None
    ) -> list[CreditBalance]:
        query = select(CreditBalance)
        if customer_id is not None:
            query = query.where(CreditBalance.customer_id == customer_id)
        return list(
            (
                await db.scalars(
                    query.order_by(CreditBalance.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_balance(record_id: int, db: AsyncSession) -> CreditBalance:
        row = await db.get(CreditBalance, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_ledger(
        db: AsyncSession, skip: int, limit: int, customer_id: UUID | None
    ) -> list[CreditLedgerEntry]:
        query = select(CreditLedgerEntry)
        if customer_id is not None:
            query = query.where(CreditLedgerEntry.customer_id == customer_id)
        return list(
            (
                await db.scalars(
                    query.order_by(CreditLedgerEntry.id.desc())
                    .offset(skip)
                    .limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_ledger(record_id: int, db: AsyncSession) -> CreditLedgerEntry:
        row = await db.get(CreditLedgerEntry, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_grant(
        db: AsyncSession, skip: int, limit: int, customer_id: UUID | None
    ) -> list[CreditGrant]:
        query = select(CreditGrant)
        if customer_id is not None:
            query = query.where(CreditGrant.customer_id == customer_id)
        return list(
            (
                await db.scalars(
                    query.order_by(CreditGrant.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_grant(record_id: int, db: AsyncSession) -> CreditGrant:
        row = await db.get(CreditGrant, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_webhook(
        db: AsyncSession, skip: int, limit: int, status: str | None
    ) -> list[StripeEvent]:
        query = select(StripeEvent)
        if status is not None:
            query = query.where(StripeEvent.status == status)
        return list(
            (
                await db.scalars(
                    query.order_by(StripeEvent.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_webhook(record_id: int, db: AsyncSession) -> StripeEvent:
        row = await db.get(StripeEvent, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def list_intent(
        db: AsyncSession,
        skip: int,
        limit: int,
        customer_id: UUID | None,
        status: str | None,
        followup: bool | None,
    ) -> list[OperationIntent]:
        query = select(OperationIntent)
        if customer_id is not None:
            query = query.where(OperationIntent.customer_id == customer_id)
        if status is not None:
            query = query.where(OperationIntent.status == status)
        if followup is not None:
            query = query.where(
                OperationIntent.followup_at.is_not(None)
                if followup
                else OperationIntent.followup_at.is_(None)
            )
        return list(
            (
                await db.scalars(
                    query.order_by(OperationIntent.id.desc()).offset(skip).limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_intent(record_id: UUID, db: AsyncSession) -> OperationIntent:
        row = await db.get(OperationIntent, record_id)
        if row is None:
            raise HTTPException(404, "Record not found")
        return row

    @staticmethod
    async def payment_states(
        db: AsyncSession, skip: int, limit: int, psp_customer_id: str | None
    ) -> list[PspPaymentState]:
        query = select(PspPaymentState)
        if psp_customer_id is not None:
            query = query.where(PspPaymentState.psp_customer_id == psp_customer_id)
        return list(
            (
                await db.scalars(
                    query.order_by(
                        PspPaymentState.updated_at.desc(),
                        PspPaymentState.psp_payment_intent_id,
                    )
                    .offset(skip)
                    .limit(limit)
                )
            ).all()
        )
