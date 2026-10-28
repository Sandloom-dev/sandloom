"""Declared read operations of the billing module (execution planner).

The module states WHAT it reads; the kernel's planner decides HOW (a generated
statement in a read-only transaction on the optimized executor, or the ORM
general form). Only billing's own entities are referenced.

``GET /subscription`` and ``GET /credits`` are pure reads: a tenant without a
billing customer answers exactly what a fresh customer answers (status
``"none"``, balance 0) and nothing is created — the customer is created only
by the paths that need it (checkout / customer-portal intents, which then
grant credits and receive webhooks). The tenant's customer is found through
its UNIQUE ``user_id`` / ``organization_id`` (one operation per tenant kind:
the column is part of the declaration, never a runtime choice).
"""

from sandloom.sdk.data import JoinRead, ListOperation
from sandloom_billing.models import (
    BillingCustomer,
    BillingPrice,
    BillingProduct,
    BillingSubscription,
    CreditBalance,
)
from sandloom_billing.schemas.reads import BalanceRow, PlanRow, SubscriptionRow

TENANT_COLUMNS = {"user": "user_id", "organization": "organization_id"}


def _subscriptions(kind: str, column: str) -> ListOperation[SubscriptionRow]:
    # newest first (``id DESC``), as the ORM read it; the primary key orders
    return ListOperation(
        entity=BillingSubscription,
        result=SubscriptionRow,
        join=BillingCustomer,
        via="customer_id",
        join_by={"tenant_id": column},
        order_by=("id",),
        descending=True,
        name=f"billing.subscriptions.of_{kind}",
    )


def _balance(kind: str, column: str) -> JoinRead[BalanceRow]:
    # balance JOIN customer: at most one row (customer_id and the tenant
    # column are both UNIQUE); no row = no customer or no balance row = 0
    return JoinRead(
        entity=CreditBalance,
        result=BalanceRow,
        by=(),
        join=BillingCustomer,
        via="customer_id",
        join_by={"tenant_id": column},
        name=f"billing.balance.of_{kind}",
    )


#: ``GET /subscription``: a tenant's subscriptions, newest first.
subscriptions_of = {
    kind: _subscriptions(kind, col) for kind, col in TENANT_COLUMNS.items()
}

#: ``GET /credits``: a tenant's materialized credit balance.
balance_of = {kind: _balance(kind, col) for kind, col in TENANT_COLUMNS.items()}

#: The plan of a subscription's price (price JOIN product).
plan_of_price = JoinRead(
    entity=BillingPrice,
    result=PlanRow,
    by=("id",),
    join=BillingProduct,
    via="product_id",
    joined_fields={"name": "name"},
    name="billing.plan.of_price",
)

#: Everything billing declares (registered at startup, see ``sandloom_billing``).
OPERATIONS = (*subscriptions_of.values(), *balance_of.values(), plan_of_price)

__all__ = [
    "OPERATIONS",
    "TENANT_COLUMNS",
    "balance_of",
    "plan_of_price",
    "subscriptions_of",
]
