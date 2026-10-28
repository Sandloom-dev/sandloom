"""Billing HTTP API: plans, subscription, credits, checkout, portal."""

import uuid

import pytest
from httpx import AsyncClient

from sandloom_billing.tests import tenancy_api


def idempotency_headers(headers: dict, key: str) -> dict:
    return {**headers, "Idempotency-Key": f"{key}-{uuid.uuid4().hex}"}


@pytest.mark.asyncio
class TestPlans:
    async def test_list_plans_public(self, async_client: AsyncClient, catalog):
        resp = await async_client.get("/api/v1/billing/plans")
        assert resp.status_code == 200
        products = resp.json()["products"]
        names = {p["name"] for p in products}
        assert {"Pro", "100 credits"} <= names
        pro = next(p for p in products if p["name"] == "Pro")
        assert pro["prices"][0]["amount"] == 9900
        assert pro["prices"][0]["recurring_interval"] == "month"


@pytest.mark.asyncio
class TestSubscriptionAndCredits:
    async def test_subscription_none_for_new_user(
        self, async_client: AsyncClient, user_a
    ):
        _, headers = user_a
        resp = await async_client.get("/api/v1/billing/subscription", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "none"

    async def test_credits_zero_for_new_user(self, async_client: AsyncClient, user_a):
        _, headers = user_a
        resp = await async_client.get("/api/v1/billing/credits", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"balance": 0, "debt": 0, "in_debt": False}

    async def test_reads_never_create_a_customer(
        self, async_client: AsyncClient, user_a, test_db
    ):
        """Both reads are pure: no customer / balance row is created for a
        tenant without one (it answers what a fresh customer answers); the
        checkout creates it."""
        from sqlalchemy import func, select

        from sandloom_billing.models import BillingCustomer, CreditBalance

        user_id, headers = user_a
        for _ in range(2):
            sub = await async_client.get(
                "/api/v1/billing/subscription", headers=headers
            )
            assert sub.status_code == 200, sub.text
            assert sub.json() == {
                "status": "none",
                "current_period_end": None,
                "trial_end": None,
                "cancel_at_period_end": False,
                "plan": None,
            }
            credits = await async_client.get("/api/v1/billing/credits", headers=headers)
            assert credits.status_code == 200, credits.text
            assert credits.json() == {"balance": 0, "debt": 0, "in_debt": False}
        for model in (BillingCustomer, CreditBalance):
            assert await test_db.scalar(select(func.count()).select_from(model)) == 0
        customers = (
            await test_db.execute(
                select(BillingCustomer.id).where(BillingCustomer.user_id == user_id)
            )
        ).all()
        assert customers == []

    async def test_subscription_read_matches_the_service(
        self, async_client: AsyncClient, user_a, test_db, catalog
    ):
        """The one-read path answers exactly what get_subscription_status does."""
        from datetime import UTC, datetime, timedelta

        from sandloom_billing.models import BillingSubscription
        from sandloom_billing.services import BillingService

        user_id, headers = user_a
        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        test_db.add_all(
            [
                BillingSubscription(
                    customer_id=customer.id,
                    price_id=catalog["pro_month"].id,
                    status="canceled",
                ),
                BillingSubscription(
                    customer_id=customer.id,
                    price_id=catalog["pro_month"].id,
                    status="active",
                    current_period_end=datetime.now(UTC) + timedelta(days=20),
                ),
            ]
        )
        await test_db.flush()
        await BillingService.add_credits(customer, 7, "grant-7", test_db)
        expected = await BillingService.get_subscription_status(customer.id, test_db)

        resp = await async_client.get("/api/v1/billing/subscription", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == expected.model_dump(mode="json")
        assert resp.json()["status"] == "active"
        assert resp.json()["plan"]["name"] == "Pro"
        credits = await async_client.get("/api/v1/billing/credits", headers=headers)
        assert credits.json() == {"balance": 7, "debt": 0, "in_debt": False}

    async def test_requires_auth(self, async_client: AsyncClient):
        for path in ("/api/v1/billing/subscription", "/api/v1/billing/credits"):
            resp = await async_client.get(path)
            assert resp.status_code == 401


@pytest.mark.asyncio
class TestCheckoutAndPortal:
    async def test_checkout_session_personal(
        self, async_client: AsyncClient, user_a, catalog, fake_psp
    ):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/billing/checkout-session",
            json={"price_id": catalog["pro_month"].id},
            headers=idempotency_headers(headers, "checkout-personal"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"].startswith("https://checkout.stripe.local/")
        # PSP got the catalog's external price id and a created customer
        assert fake_psp.checkout_sessions[0]["price"] == "price_pro_month"
        assert fake_psp.checkout_sessions[0]["mode"] == "subscription"
        assert len(fake_psp.created_customers) == 1

    async def test_checkout_one_time_uses_payment_mode(
        self, async_client: AsyncClient, user_a, catalog, fake_psp
    ):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/billing/checkout-session",
            json={"price_id": catalog["pack_price"].id},
            headers=idempotency_headers(headers, "checkout-one-time"),
        )
        assert resp.status_code == 200
        assert fake_psp.checkout_sessions[0]["mode"] == "payment"

    async def test_checkout_unknown_price_404(self, async_client: AsyncClient, user_a):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/billing/checkout-session",
            json={"price_id": 99999},
            headers=idempotency_headers(headers, "checkout-missing-price"),
        )
        assert resp.status_code == 404

    async def test_checkout_requires_idempotency_key(
        self, async_client: AsyncClient, user_a, catalog
    ):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/billing/checkout-session",
            json={"price_id": catalog["pro_month"].id},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "Idempotency-Key" in resp.text

    async def test_customer_portal(self, async_client: AsyncClient, user_a, fake_psp):
        _, headers = user_a
        resp = await async_client.post(
            "/api/v1/billing/customer-portal",
            json={},
            headers=idempotency_headers(headers, "portal"),
        )
        assert resp.status_code == 200
        assert resp.json()["url"].startswith("https://billing.stripe.local/")

    async def test_psp_customer_created_once(
        self, async_client: AsyncClient, user_a, fake_psp
    ):
        _, headers = user_a
        for idx in range(2):
            resp = await async_client.post(
                "/api/v1/billing/customer-portal",
                json={},
                headers=idempotency_headers(headers, f"portal-customer-once-{idx}"),
            )
            assert resp.status_code == 200
        assert len(fake_psp.created_customers) == 1

    async def test_fake_psp_replays_session_for_same_idempotency_key(self, fake_psp):
        kwargs = {
            "customer_id": "cus_123",
            "price_external_id": "price_123",
            "success_url": "https://app.test/success",
            "cancel_url": "https://app.test/cancel",
            "idempotency_key": "psp-checkout-key",
        }
        first = await fake_psp.create_checkout_session(**kwargs)
        second = await fake_psp.create_checkout_session(**kwargs)
        assert second == first
        assert len(fake_psp.checkout_sessions) == 1


@pytest.mark.asyncio
class TestOrgContext:
    async def test_unknown_org_id_403(self, async_client: AsyncClient, user_a):
        """org_id passed but tenancy has no membership → 403."""
        _, headers = user_a
        resp = await async_client.get(
            f"/api/v1/billing/subscription?org_id={uuid.uuid4()}", headers=headers
        )
        assert resp.status_code == 403

    async def test_member_org_context_ok(
        self, async_client: AsyncClient, user_a, test_db
    ):
        """Member of an org can query its billing context explicitly."""

        _user_id, headers = user_a
        org_id = await tenancy_api.create_org(async_client, headers, name="B2B Co")
        resp = await async_client.get(
            f"/api/v1/billing/subscription?org_id={org_id}", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "none"

    async def test_single_org_implies_org_context(
        self, async_client: AsyncClient, user_a, test_db
    ):
        """User in exactly one org: without org_id the PERSONAL account answers
        (spec §7.4, no inference); the org's account needs org_id."""
        from sandloom_billing.services import BillingService

        _user_id, headers = user_a
        org_id = await tenancy_api.create_org(async_client, headers, name="Solo Co")
        customer = await BillingService.get_or_create_customer(
            test_db, organization_id=org_id
        )
        await BillingService.add_credits(customer, 55, "org-grant", test_db)

        resp = await async_client.get("/api/v1/billing/credits", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"balance": 0, "debt": 0, "in_debt": False}
        resp = await async_client.get(
            f"/api/v1/billing/credits?org_id={org_id}", headers=headers
        )
        assert resp.json() == {"balance": 55, "debt": 0, "in_debt": False}

    async def test_multiple_orgs_without_org_id_422(
        self, async_client: AsyncClient, user_a, test_db
    ):

        _user_id, headers = user_a
        for i in range(2):
            await tenancy_api.create_org(async_client, headers, name=f"Org {i}")
        resp = await async_client.get("/api/v1/billing/credits", headers=headers)
        # no ambiguity left: the personal account (spec §7.4)
        assert resp.status_code == 200
        assert resp.json()["balance"] == 0
