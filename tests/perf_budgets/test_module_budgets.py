"""Round-trip / contract-call budgets of the bundled modules' hot services.

* billing spend: debit RETURNING + ledger INSERT carrying balance_after inside
  one savepoint (was INSERT ledger + debit + UPDATE balance_after);
* billing claims contributor: customer + subscriptions in ONE read (was 2),
  plan info in ONE joined read (was price + product + a prices selectin);
* billing tenant resolution: ONE membership contract call for an org context
  with a minimum role (was get_role + has_role_at_least — each a Unit of Work
  on the tenancy database).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.drivers.sqlalchemy_database import build_disposable_engine
from sandloom.core.testing import bind_test_uow
from sandloom_billing.entitlements import EntitlementsService
from sandloom_billing.models import (
    INTERVAL_MONTH,
    PRODUCT_TYPE_SUBSCRIPTION,
    BillingPrice,
    BillingProduct,
    BillingSubscription,
)
from sandloom_billing.services import BillingService
from sandloom_billing.services.tenant_resolver import (
    BILLING_MANAGER_ROLE,
    resolve_tenant,
)

from .conftest import proxied_url
from .pgcount import PgCountingProxy

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def counted_sessions(
    db_engine: Any, pg_proxy: PgCountingProxy
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_disposable_engine(proxied_url(pg_proxy))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _customer_with_credits(
    sessions: async_sessionmaker[AsyncSession], amount: int
) -> Any:
    async with sessions() as s, s.begin():
        with bind_test_uow(s):
            customer = await BillingService.get_or_create_customer(
                s, user_id=uuid.uuid4()
            )
            await BillingService.add_credits(customer, amount, "seed", s)
    return customer


async def test_spend_credits_budget(
    counted_sessions: async_sessionmaker[AsyncSession], pg_proxy: PgCountingProxy
) -> None:
    customer = await _customer_with_credits(counted_sessions, 100)
    async with counted_sessions() as s, s.begin():
        with bind_test_uow(s):
            await BillingService.spend_credits(customer, 1, "warm", s)  # prepare
            await s.flush()  # the warm-up's outbox row
            pg_proxy.reset()
            balance = await BillingService.spend_credits(customer, 10, "op-1", s)
            trace = pg_proxy.reset()
    assert balance == 89
    # SAVEPOINT, UPDATE balance RETURNING, INSERT ledger, RELEASE.
    assert len(trace) <= 4, pg_proxy.report(trace)


async def test_spend_replay_and_overdraft_keep_their_semantics(
    counted_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom_billing.exceptions import (
        CreditOperationConflictError,
        InsufficientCreditsError,
    )

    customer = await _customer_with_credits(counted_sessions, 10)
    async with counted_sessions() as s, s.begin():
        with bind_test_uow(s):
            assert await BillingService.spend_credits(customer, 8, "k", s) == 2
            # Replay returns the ORIGINAL result although 8 > balance now.
            assert await BillingService.spend_credits(customer, 8, "k", s) == 2
            with pytest.raises(CreditOperationConflictError):
                await BillingService.spend_credits(customer, 1, "k", s)
            with pytest.raises(InsufficientCreditsError):
                await BillingService.spend_credits(customer, 5, "k2", s)
            assert await BillingService.get_credit_balance(customer.id, s) == 2


async def test_subscription_claims_budget(
    counted_sessions: async_sessionmaker[AsyncSession], pg_proxy: PgCountingProxy
) -> None:
    user_id = uuid.uuid4()
    async with counted_sessions() as s, s.begin():
        product = BillingProduct(name="Pro", type=PRODUCT_TYPE_SUBSCRIPTION)
        s.add(product)
        await s.flush()
        price = BillingPrice(
            product_id=product.id,
            amount=900,
            currency="USD",
            recurring_interval=INTERVAL_MONTH,
        )
        s.add(price)
        customer = await BillingService.get_or_create_customer(s, user_id=user_id)
        await s.flush()
        s.add(
            BillingSubscription(
                customer_id=customer.id,
                price_id=price.id,
                status="active",
                origin="intent",  # authorized (ADR-0003 item 9)
                current_period_end=datetime.now(UTC) + timedelta(days=3),
            )
        )
    async with counted_sessions() as s, s.begin():
        await EntitlementsService.subscription_claims("user", user_id, s)  # prepare
        pg_proxy.reset()
        claims = await EntitlementsService.subscription_claims("user", user_id, s)
        trace = pg_proxy.reset()
        assert claims == {"subscription_status": "active", "plan_code": "Pro"}
        # customer+subscriptions (1) + plan info (1); was 2 + 3.
        assert len(trace) <= 2, pg_proxy.report(trace)

        pg_proxy.reset()
        assert (
            await EntitlementsService.subscription_claims("user", uuid.uuid4(), s) == {}
        )
        assert len(pg_proxy.reset()) <= 1


class _Membership:
    def __init__(self, role: str | None) -> None:
        self.role = role
        self.calls: list[str] = []

    async def get_role(self, org_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        self.calls.append("get_role")
        return self.role

    async def has_role_at_least(
        self, org_id: uuid.UUID, user_id: uuid.UUID, minimum: str
    ) -> bool:
        self.calls.append("has_role_at_least")
        return False


class _Core:
    def __init__(self, membership: _Membership) -> None:
        self.membership = membership

    def get_service(self, name: str) -> Any:
        return self.membership


@pytest.mark.parametrize(
    ("role", "allowed"), [("owner", True), ("admin", True), ("member", False)]
)
async def test_org_tenant_with_minimum_role_is_one_contract_call(
    role: str, allowed: bool
) -> None:
    from fastapi import HTTPException

    membership = _Membership(role)
    try:
        await resolve_tenant(
            core=_Core(membership),
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            db=None,  # type: ignore[arg-type]
            minimum_role=BILLING_MANAGER_ROLE,
        )
        assert allowed
    except HTTPException as exc:
        assert not allowed and exc.status_code == 403
    assert membership.calls == ["get_role"]


async def test_unknown_role_falls_back_to_the_contract() -> None:
    """A role billing does not know is decided by tenancy (fail-closed)."""
    from fastapi import HTTPException

    membership = _Membership("billing-viewer")
    with pytest.raises(HTTPException) as info:
        await resolve_tenant(
            core=_Core(membership),
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            db=None,  # type: ignore[arg-type]
            minimum_role=BILLING_MANAGER_ROLE,
        )
    assert info.value.status_code == 403
    assert membership.calls == ["get_role", "has_role_at_least"]
