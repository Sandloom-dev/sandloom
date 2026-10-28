"""Billing DTOs (BILLING_SPEC §10)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stripe_price_id: str | None
    amount: int
    currency: str
    recurring_interval: str | None
    credits: int | None = None  # credits granted by one purchase (credit packs)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    prices: list[PriceOut] = Field(default_factory=list)


class PlansOut(BaseModel):
    products: list[ProductOut]


class PlanInfo(BaseModel):
    product_id: int
    price_id: int
    name: str
    interval: str | None


class SubscriptionOut(BaseModel):
    status: str
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    plan: PlanInfo | None = None


class CreditsOut(BaseModel):
    """A tenant's credits. ``balance`` MAY BE NEGATIVE: credits already spent
    from a purchase that was later refunded / disputed are owed (ADR-0003
    item 12). UIs should show a negative balance as a debt (``in_debt``,
    ``debt`` = credits owed), not as an error; spending is refused until
    purchases cover it."""

    balance: int
    #: Credits owed (``max(0, -balance)``).
    debt: int = 0
    in_debt: bool = False

    @classmethod
    def of(cls, balance: int) -> "CreditsOut":
        return cls(balance=balance, debt=max(0, -balance), in_debt=balance < 0)


class CheckoutSessionIn(BaseModel):
    price_id: int
    org_id: UUID | None = None


class CustomerPortalIn(BaseModel):
    org_id: UUID | None = None


class SessionUrlOut(BaseModel):
    url: str


class SpendCreditsIn(BaseModel):
    """Internal/programmatic spend (consuming domains call the service)."""

    amount: int = Field(gt=0)
    reason: str = ""
    operation_key: str = Field(min_length=1, max_length=255)
    org_id: UUID | None = None
