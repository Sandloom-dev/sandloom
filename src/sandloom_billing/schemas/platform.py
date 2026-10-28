"""Whitelisted operator views; never serialize ORM rows wholesale."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OperatorView(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PlatformCustomer(OperatorView):
    id: UUID
    user_id: UUID | None
    organization_id: UUID | None
    stripe_customer_id: str | None
    tenant_type: str
    tenant_id: UUID


class PlatformSubscription(OperatorView):
    id: int
    customer_id: UUID
    price_id: int | None
    stripe_subscription_id: str | None
    status: str
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    origin: str
    intent_id: UUID | None


class PlatformProduct(OperatorView):
    id: int
    name: str
    type: str
    is_active: bool
    stripe_product_id: str | None


class PlatformPrice(OperatorView):
    id: int
    product_id: int
    amount: int
    currency: str
    recurring_interval: str | None
    credits: int | None
    stripe_price_id: str | None


class PlatformBalance(OperatorView):
    id: int
    customer_id: UUID
    amount: int


class PlatformLedger(OperatorView):
    id: int
    customer_id: UUID
    delta: int
    reason: str
    operation_key: str
    balance_after: int | None
    created_at: datetime


class PlatformGrant(OperatorView):
    id: int
    customer_id: UUID
    psp_session_id: str
    psp_payment_intent_id: str | None
    price_id: int
    credits: int
    amount: int
    currency: str
    reversed: int
    created_at: datetime


class PlatformWebhook(OperatorView):
    id: int
    event_id: str
    status: str
    received_at: datetime


class PlatformIntent(OperatorView):
    id: UUID
    customer_id: UUID
    tenant_type: str
    tenant_id: UUID
    actor_id: UUID
    operation: str
    status: str
    price_id: int | None
    psp_session_id: str | None
    issued_at: datetime | None
    psp_expired_at: datetime | None
    reconcile_attempts: int
    followup_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CreditAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["grant", "deduct"]
    amount: int = Field(strict=True, gt=0, le=1_000_000_000_000)
    reason: str = Field(min_length=8, max_length=300, pattern=r".*\S.*")
    operation_key: UUID


class CreditAdjustmentResult(BaseModel):
    id: UUID
    balance_after: int
    operation_key: UUID
    replayed: bool


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255, pattern=r".*\S.*")
    is_active: bool
    reason: str = Field(min_length=8, max_length=300, pattern=r".*\S.*")


class PlatformPaymentState(OperatorView):
    id: str
    psp_customer_id: str | None
    currency: str | None
    amount_refunded: int
    dispute_status: str | None
    dispute_amount: int
    updated_at: datetime


class ProviderInfo(BaseModel):
    provider: Literal["fake", "stripe"]
    mode: Literal["demo", "test", "live"]
    dashboard_url: str | None
