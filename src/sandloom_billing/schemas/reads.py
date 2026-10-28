"""Row DTOs of billing's declared reads (execution planner; internal).

Not part of the HTTP API: the routes still answer ``SubscriptionOut`` /
``CreditsOut``. Each DTO is exactly the columns one declared operation selects
(``services.operations``), so the optimized executor reads nothing else.
"""

from datetime import datetime

from pydantic import BaseModel


class SubscriptionRow(BaseModel):
    """What ``GET /subscription`` needs of one subscription: the access
    predicate's inputs (``subscription_grants_access``) and the answer."""

    id: int
    price_id: int | None
    stripe_subscription_id: str | None
    status: str
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at_period_end: bool
    origin: str


class PlanRow(BaseModel):
    """A catalog price with its product's name (``PlanInfo``'s source)."""

    id: int
    recurring_interval: str | None
    product_id: int
    name: str


class BalanceRow(BaseModel):
    """A customer's materialized credit balance."""

    amount: int
