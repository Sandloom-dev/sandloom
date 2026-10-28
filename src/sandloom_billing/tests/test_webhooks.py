"""Stripe webhook tests: signature, idempotency, subscription lifecycle."""

import json
import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.services import BillingService
from sandloom_billing.tests.conftest import WEBHOOK_SECRET


@pytest.fixture
def _intentless_subscriptions_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in for the tests below that assert ACCESS from an intent-less
    event (no ``sandloom_intent_id``): granting those is an explicit policy
    (ADR-0003 open item 9, default off). Every other test in this module runs
    under the default (intent-less subscriptions grant nothing)."""
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "billing_grant_intentless_subscriptions", True)


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)}


def _subscription_event(
    event_id: str,
    event_type: str,
    *,
    customer: str,
    sub_id: str = "sub_test_1",
    status: str = "active",
    period_end: int | None = None,
    trial_end: int | None = None,
    created: int | None = None,
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "created": created or int(time.time()),
        "data": {
            "object": {
                "id": sub_id,
                "customer": customer,
                "status": status,
                "current_period_end": period_end or int(time.time()) + 86400 * 30,
                "trial_end": trial_end,
                "cancel_at_period_end": False,
                "items": {"data": [{"price": {"id": "price_pro_month"}}]},
            }
        },
    }


@pytest.mark.asyncio
class TestWebhookSecurity:
    async def test_invalid_signature_rejected(self, async_client: AsyncClient):
        body = json.dumps({"id": "evt_x", "type": "noop"}).encode()
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert resp.status_code == 400

    async def test_missing_signature_rejected(self, async_client: AsyncClient):
        body = json.dumps({"id": "evt_x", "type": "noop"}).encode()
        resp = await async_client.post("/api/v1/billing/webhooks/stripe", content=body)
        assert resp.status_code == 400

    async def test_stale_timestamp_rejected(self, async_client: AsyncClient):
        body = json.dumps({"id": "evt_x", "type": "noop"}).encode()
        stale = sign_stripe_payload(
            body, WEBHOOK_SECRET, timestamp=int(time.time()) - 3600
        )
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": stale},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestWebhookProcessing:
    async def _make_psp_customer(
        self, test_db: AsyncSession, external_id: str = "cus_hooked"
    ):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        customer.stripe_customer_id = external_id
        await test_db.commit()
        return customer

    @pytest.mark.usefixtures("_intentless_subscriptions_grant")
    async def test_subscription_created_activates_access(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        customer = await self._make_psp_customer(test_db)
        body, headers = _signed(
            _subscription_event(
                "evt_sub_created",
                "customer.subscription.created",
                customer="cus_hooked",
            )
        )
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "processed"

        status_out = await BillingService.get_subscription_status(customer.id, test_db)
        assert status_out.status == "active"
        assert status_out.plan is not None and status_out.plan.name == "Pro"
        assert await BillingService.check_subscription_active(customer.id, test_db)

    async def test_webhook_idempotent_replay(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        await self._make_psp_customer(test_db)
        body, headers = _signed(
            _subscription_event(
                "evt_replay", "customer.subscription.created", customer="cus_hooked"
            )
        )
        first = await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )
        second = await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )
        assert first.status_code == 200 and first.json()["status"] == "processed"
        assert second.status_code == 200 and second.json()["status"] == "duplicate"

    async def test_subscription_deleted_revokes_access(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        customer = await self._make_psp_customer(test_db)
        created, h1 = _signed(
            _subscription_event(
                "evt_c1", "customer.subscription.created", customer="cus_hooked"
            )
        )
        await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=created, headers=h1
        )
        deleted, h2 = _signed(
            _subscription_event(
                "evt_c2",
                "customer.subscription.deleted",
                customer="cus_hooked",
                status="canceled",
            )
        )
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=deleted, headers=h2
        )
        assert resp.status_code == 200
        assert not await BillingService.check_subscription_active(customer.id, test_db)

    @pytest.mark.usefixtures("_intentless_subscriptions_grant")
    async def test_trialing_grants_access(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        customer = await self._make_psp_customer(test_db)
        body, headers = _signed(
            _subscription_event(
                "evt_trial",
                "customer.subscription.created",
                customer="cus_hooked",
                status="trialing",
                trial_end=int(time.time()) + 86400 * 7,
            )
        )
        await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )
        assert await BillingService.check_subscription_active(customer.id, test_db)

    @pytest.mark.usefixtures("_intentless_subscriptions_grant")
    async def test_past_due_policy(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        catalog,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from sandloom.sdk.infra import settings

        customer = await self._make_psp_customer(test_db)
        body, headers = _signed(
            _subscription_event(
                "evt_pd",
                "customer.subscription.created",
                customer="cus_hooked",
                status="past_due",
            )
        )
        await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )

        monkeypatch.setattr(settings, "billing_deny_on_past_due", False)
        assert await BillingService.check_subscription_active(customer.id, test_db)

        monkeypatch.setattr(settings, "billing_deny_on_past_due", True)
        assert not await BillingService.check_subscription_active(customer.id, test_db)

    async def test_checkout_completed_grants_credits(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ):
        # Previously this test injected ``metadata.credits`` by hand, masking
        # that real checkouts never carried it (no credits were ever granted).
        # The grant now comes from the purchased price's catalog ``credits``,
        # with the metadata exactly as BillingService.create_checkout_session
        # writes it.
        customer = await self._make_psp_customer(test_db)
        payload = {
            "id": "evt_checkout",
            "type": "checkout.session.completed",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": "cs_paid_1",
                    "mode": "payment",
                    "payment_status": "paid",
                    "payment_intent": "pi_paid_1",
                    "amount_total": catalog["pack_price"].amount,
                    "currency": "usd",
                    "customer": "cus_hooked",
                    "metadata": {
                        "customer_id": str(customer.id),
                        "price_id": str(catalog["pack_price"].id),
                    },
                }
            },
        }
        body, headers = _signed(payload)
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )
        assert resp.status_code == 200
        assert await BillingService.get_credit_balance(customer.id, test_db) == 100

    async def test_unknown_event_type_ignored(self, async_client: AsyncClient):
        body, headers = _signed({"id": "evt_unknown", "type": "balance.available"})
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe", content=body, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
