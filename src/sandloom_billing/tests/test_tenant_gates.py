"""Tenant gates: org billing management roles, isolation, org access fact, claims."""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import require
from sandloom.sdk.authz import Category, Entitled
from sandloom.sdk.contracts import contract_method
from sandloom.sdk.testing import bind_contract_session
from sandloom_billing.models import BillingSubscription
from sandloom_billing.services import BillingService
from sandloom_billing.tests import tenancy_api
from sandloom_billing.tests.conftest import make_user_headers


def _idem(headers: dict, key: str) -> dict:
    return {**headers, "Idempotency-Key": f"{key}-{uuid.uuid4().hex}"}


async def _org_with_member(
    client: AsyncClient, test_db: AsyncSession, member_role: str
) -> tuple[uuid.UUID, tuple[uuid.UUID, dict], tuple[uuid.UUID, dict]]:
    owner = make_user_headers()
    member = make_user_headers()
    org_id = await tenancy_api.create_org(client, owner[1], name="Acme")
    await tenancy_api.add_member(client, org_id, owner[1], member[0], member_role)
    await test_db.commit()
    return org_id, owner, member


async def _catalog_price(test_db: AsyncSession) -> int:
    """A catalog price: a subscription to no known price grants nothing."""
    from sandloom_billing.models import (
        INTERVAL_MONTH,
        PRODUCT_TYPE_SUBSCRIPTION,
        BillingPrice,
        BillingProduct,
    )

    product = BillingProduct(name="Gate", type=PRODUCT_TYPE_SUBSCRIPTION)
    test_db.add(product)
    await test_db.flush()
    price = BillingPrice(
        product_id=product.id,
        amount=100,
        currency="USD",
        recurring_interval=INTERVAL_MONTH,
        stripe_price_id=f"price_gate_{uuid.uuid4().hex[:8]}",
    )
    test_db.add(price)
    await test_db.flush()
    return int(price.id)


async def _activate(
    test_db: AsyncSession, customer_id: uuid.UUID, *, period_end: datetime | None
) -> None:
    test_db.add(
        BillingSubscription(
            customer_id=customer_id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex[:10]}",
            status="active",
            price_id=await _catalog_price(test_db),
            origin="intent",  # an authorized subscription (ADR-0003 item 9)
            current_period_end=period_end,
        )
    )
    await test_db.commit()


@pytest.mark.asyncio
class TestOrgBillingManagement:
    async def test_plain_member_cannot_checkout_or_open_portal(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        org, _, (_, member_headers) = await _org_with_member(
            async_client, test_db, "member"
        )
        resp = await async_client.post(
            "/api/v1/billing/checkout-session",
            json={"price_id": catalog["pro_month"].id, "org_id": str(org)},
            headers=_idem(member_headers, "co"),
        )
        assert resp.status_code == 403, resp.text
        resp = await async_client.post(
            "/api/v1/billing/customer-portal",
            json={"org_id": str(org)},
            headers=_idem(member_headers, "portal"),
        )
        assert resp.status_code == 403
        # ...but may still READ the org's billing.
        resp = await async_client.get(
            f"/api/v1/billing/subscription?org_id={org}", headers=member_headers
        )
        assert resp.status_code == 200

    async def test_no_org_id_never_manages_the_single_org(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        """Without org_id a member manages their PERSONAL account, never the
        single organization they belong to (spec §7.4)."""
        from sqlalchemy import func, select

        from sandloom_billing.models import BillingCustomer

        org, _, (_, member_headers) = await _org_with_member(
            async_client, test_db, "member"
        )

        async def org_customers() -> int:
            return int(
                await test_db.scalar(
                    select(func.count())
                    .select_from(BillingCustomer)
                    .where(BillingCustomer.organization_id == org)
                )
            )

        before = await org_customers()
        resp = await async_client.post(
            "/api/v1/billing/customer-portal",
            json={},
            headers=_idem(member_headers, "portal-implicit"),
        )
        assert resp.status_code == 200, resp.text
        assert await org_customers() == before

    @pytest.mark.parametrize("role", ["admin"])
    async def test_admin_and_owner_can_manage(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog, role: str
    ):
        org, (_, owner_headers), (_, admin_headers) = await _org_with_member(
            async_client, test_db, role
        )
        for headers in (owner_headers, admin_headers):
            resp = await async_client.post(
                "/api/v1/billing/customer-portal",
                json={"org_id": str(org)},
                headers=_idem(headers, "portal-ok"),
            )
            assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
class TestTenantIsolation:
    async def test_users_never_see_each_others_credits(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        a_id, a_headers = make_user_headers()
        _, b_headers = make_user_headers()
        customer = await BillingService.get_or_create_customer(test_db, user_id=a_id)
        await BillingService.add_credits(customer, 42, "iso-grant", test_db)
        await test_db.commit()
        a = await async_client.get("/api/v1/billing/credits", headers=a_headers)
        b = await async_client.get("/api/v1/billing/credits", headers=b_headers)
        assert a.json() == {"balance": 42, "debt": 0, "in_debt": False}
        assert b.json() == {"balance": 0, "debt": 0, "in_debt": False}

    async def test_non_member_cannot_read_org_billing(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        org, _, _ = await _org_with_member(async_client, test_db, "member")
        customer = await BillingService.get_or_create_customer(
            test_db, organization_id=org
        )
        await BillingService.add_credits(customer, 7, "org-iso", test_db)
        await test_db.commit()
        _, outsider = make_user_headers()
        resp = await async_client.get(
            f"/api/v1/billing/credits?org_id={org}", headers=outsider
        )
        assert resp.status_code == 403


# --- organization.subscription.active access fact ----------------------------

_org_router = APIRouter(prefix="/api/v1/_billing_org_demo")


@_org_router.get(
    "/{org_id}/premium",
    # No explicit category: the fact's own deny_category (402) applies.
    dependencies=[Depends(require(Entitled("organization.subscription.active")))],
)
async def _org_premium(org_id: uuid.UUID) -> dict[str, str]:
    return {"ok": str(org_id)}


@_org_router.get(
    "/premium",
    dependencies=[Depends(require(Entitled("organization.subscription.active")))],
)
async def _org_premium_no_org() -> dict[str, str]:
    return {"ok": "?"}


@_org_router.get(
    "/personal",
    dependencies=[Depends(require(Entitled("subscription.active")))],
)
async def _personal_premium() -> dict[str, str]:
    return {"ok": "personal"}


@_org_router.get(
    "/personal-forbidden",
    dependencies=[
        Depends(require(Entitled("subscription.active", category=Category.FORBIDDEN)))
    ],
)
async def _personal_premium_override() -> dict[str, str]:
    return {"ok": "personal"}


@pytest.fixture(autouse=True, scope="module")
def _mount_org_router(sandloom_app: Any) -> Any:
    app = sandloom_app
    app.include_router(_org_router)
    yield
    app.router.routes = [
        r
        for r in app.router.routes
        if not getattr(r, "path", "").startswith("/api/v1/_billing_org_demo")
    ]


@pytest.mark.asyncio
class TestOrgAccessFact:
    async def test_org_gate_follows_the_org_subscription(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        org, (owner_id, owner_headers), (_, member_headers) = await _org_with_member(
            async_client, test_db, "member"
        )
        url = f"/api/v1/_billing_org_demo/{org}/premium"
        # Without an org subscription the fact denies with PAYMENT_REQUIRED.
        assert (await async_client.get(url, headers=member_headers)).status_code == 402

        # A PERSONAL subscription of the member must not open the org gate.
        personal = await BillingService.get_or_create_customer(
            test_db, user_id=owner_id
        )
        await _activate(
            test_db, personal.id, period_end=datetime.now(UTC) + timedelta(1)
        )
        assert (await async_client.get(url, headers=owner_headers)).status_code == 402

        customer = await BillingService.get_or_create_customer(
            test_db, organization_id=org
        )
        await _activate(
            test_db, customer.id, period_end=datetime.now(UTC) + timedelta(1)
        )
        assert (await async_client.get(url, headers=member_headers)).status_code == 200

        # A non-member is refused as forbidden (403), not "payment required".
        _, outsider = make_user_headers()
        assert (await async_client.get(url, headers=outsider)).status_code == 403

    async def test_no_org_in_context_fails_closed(self, async_client: AsyncClient):
        _, headers = make_user_headers()
        resp = await async_client.get(
            "/api/v1/_billing_org_demo/premium", headers=headers
        )
        assert resp.status_code == 403

    async def test_personal_subscription_fact_denies_402(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        """Entitled("subscription.active") denies 402 like Subscribed /
        SubscriptionActive / Credits (the fact declares PAYMENT_REQUIRED);
        an explicit Entitled(category=...) still overrides it."""
        user_id, headers = make_user_headers()
        url = "/api/v1/_billing_org_demo/personal"
        assert (await async_client.get(url, headers=headers)).status_code == 402
        override = "/api/v1/_billing_org_demo/personal-forbidden"
        assert (await async_client.get(override, headers=headers)).status_code == 403

        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await _activate(
            test_db, customer.id, period_end=datetime.now(UTC) + timedelta(1)
        )
        assert (await async_client.get(url, headers=headers)).status_code == 200


@pytest.mark.asyncio
class TestClaimLiveParity:
    async def test_expired_active_subscription_claims_expired(
        self, test_db: AsyncSession
    ):
        from sandloom_billing.claims import contribute_billing_claims
        from sandloom_billing.entitlements import EntitlementsService

        user_id = uuid.uuid4()
        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await _activate(
            test_db, customer.id, period_end=datetime.now(UTC) - timedelta(1)
        )
        with bind_contract_session(test_db):
            claims = await contract_method(contribute_billing_claims)(user_id)
        live = await EntitlementsService.check_subscription_active(
            "user", user_id, test_db
        )
        assert live is False
        assert claims["subscription_status"] == "expired"
        assert "plan_code" not in claims

    async def test_subscribed_claims_read_the_plan_in_the_same_statement(
        self, test_db: AsyncSession
    ):
        """Customer, subscriptions AND the current plan: one SELECT (was two)."""
        from sqlalchemy import event

        from sandloom_billing.claims import contribute_billing_claims

        user_id = uuid.uuid4()
        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await _activate(
            test_db, customer.id, period_end=datetime.now(UTC) + timedelta(1)
        )
        statements: list[str] = []

        def _count(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
            statements.append(sql)

        engine = test_db.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", _count)
        try:
            with bind_contract_session(test_db):
                claims = await contract_method(contribute_billing_claims)(user_id)
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert claims == {"subscription_status": "active", "plan_code": "Gate"}
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 1, statements

    async def test_older_active_sub_wins_over_newer_canceled(
        self, test_db: AsyncSession
    ):
        from sandloom_billing.claims import contribute_billing_claims

        user_id = uuid.uuid4()
        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await _activate(
            test_db, customer.id, period_end=datetime.now(UTC) + timedelta(1)
        )
        test_db.add(
            BillingSubscription(
                customer_id=customer.id,
                stripe_subscription_id=f"sub_{int(time.time())}_c",
                status="canceled",
            )
        )
        await test_db.commit()
        with bind_contract_session(test_db):
            claims = await contract_method(contribute_billing_claims)(user_id)
        assert claims["subscription_status"] == "active"
        assert await BillingService.check_subscription_active(customer.id, test_db)


class TestClaimInterpretation:
    def test_past_due_claim_follows_grace_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sandloom.sdk.infra import settings
        from sandloom_billing.access import subscription_claim_rule

        monkeypatch.setattr(settings, "billing_deny_on_past_due", False)
        assert subscription_claim_rule().interpret("past_due") is True
        monkeypatch.setattr(settings, "billing_deny_on_past_due", True)
        assert subscription_claim_rule().interpret("past_due") is False
        assert subscription_claim_rule().interpret("expired") is False
