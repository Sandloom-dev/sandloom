"""Cross-domain guards and service registry contracts."""

import json
import time
import uuid
from typing import Any

import pytest
from fastapi import APIRouter, Depends, HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import Credits, SubscriptionActive, require
from sandloom.shared.authz import Account
from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.services import BillingService
from sandloom_billing.tests.conftest import WEBHOOK_SECRET, make_user_headers

# A consuming-domain router protected by the shared authz algebra (live atoms)
_demo_router = APIRouter(prefix="/api/v1/_billing_guard_demo")


@_demo_router.get(
    "/premium", dependencies=[Depends(require(SubscriptionActive(of=Account.user())))]
)
async def _premium_endpoint() -> dict[str, str]:
    return {"ok": "premium"}


@_demo_router.get(
    "/spendy",
    dependencies=[Depends(require(Credits(50, of=Account.user()), status_code=402))],
)
async def _spendy_endpoint() -> dict[str, str]:
    return {"ok": "spendy"}


@pytest.fixture(autouse=True, scope="module")
def _mount_demo_router(sandloom_app: Any):
    app = sandloom_app

    app.include_router(_demo_router)
    yield
    # FastAPI has no public unmount; rebuilding routes keeps test isolation
    app.router.routes = [
        r
        for r in app.router.routes
        if not getattr(r, "path", "").startswith("/api/v1/_billing_guard_demo")
    ]


@pytest.mark.asyncio
class TestSharedGuards:
    async def test_require_active_subscription_blocks_without_sub(
        self, async_client: AsyncClient, user_a
    ):
        _, headers = user_a
        resp = await async_client.get(
            "/api/v1/_billing_guard_demo/premium", headers=headers
        )
        # SubscriptionActive denies with PAYMENT_REQUIRED; guards now honour it.
        assert resp.status_code == 402

    async def test_require_active_subscription_allows_subscriber(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        catalog,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from sandloom.sdk.infra import settings

        # An intent-less (PSP-created) subscription grants only under the
        # explicit policy (ADR-0003 open item 9).
        monkeypatch.setattr(settings, "billing_grant_intentless_subscriptions", True)
        user_id, headers = make_user_headers()
        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        customer.stripe_customer_id = "cus_guarded"
        await test_db.commit()
        payload = {
            "id": f"evt_{uuid.uuid4().hex[:8]}",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": f"sub_{uuid.uuid4().hex[:8]}",
                    "customer": "cus_guarded",
                    "status": "active",
                    "current_period_end": int(time.time()) + 86400,
                    # A known catalog price: an unknown one grants nothing.
                    "items": {"data": [{"price": {"id": "price_pro_month"}}]},
                }
            },
        }
        body = json.dumps(payload).encode()
        await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)},
        )
        resp = await async_client.get(
            "/api/v1/_billing_guard_demo/premium", headers=headers
        )
        assert resp.status_code == 200

    async def test_require_credits_402_then_passes(
        self, async_client: AsyncClient, test_db: AsyncSession
    ):
        user_id, headers = make_user_headers()
        resp = await async_client.get(
            "/api/v1/_billing_guard_demo/spendy", headers=headers
        )
        assert resp.status_code == 402

        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await BillingService.add_credits(customer, 50, "guard-grant", test_db)
        resp = await async_client.get(
            "/api/v1/_billing_guard_demo/spendy", headers=headers
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestServiceRegistryContracts:
    async def test_billing_services_registered(self, sandloom_core: Any):
        from sandloom_billing import ENTITLEMENTS_SERVICE_V1

        core = sandloom_core

        assert core.service_registry.contains(ENTITLEMENTS_SERVICE_V1)

    async def test_psp_client_is_not_a_public_or_rpc_contract(
        self, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
    ):
        """The PSP client is billing-internal: never in the service registry
        (so no other module can ``get_service`` it) and never reachable over
        internal RPC (``/internal/services/billing.psp.v1/...`` -> not found)."""
        import sandloom_billing as billing
        from sandloom.sdk.errors import ServiceNotFound
        from sandloom.sdk.infra import settings
        from sandloom.sdk.testing import (
            in_process_rpc_client,
            rpc_proxy_for,
            rpc_settings,
        )
        from sandloom_billing.psp import StripeClientPort, active_psp_client

        core = sandloom_core

        assert not hasattr(billing, "PSP_SERVICE_V1")
        assert not core.service_registry.contains("billing.psp.v1")
        for name in core.service_registry.list():
            service = core.service_registry.get(name)
            assert not isinstance(service, StripeClientPort), name
        with pytest.raises(ServiceNotFound):
            core.get_service("billing.psp.v1")
        # Over internal RPC the provider does not know it either.
        # (a caller allowed to call it, so the refusal is the provider's own)
        keyed = rpc_settings("billing", allowed_calls={"billing": ["billing.psp.v1"]})
        for name, value in keyed.items():
            monkeypatch.setattr(settings, name, value)
        async with in_process_rpc_client(core) as client:
            remote = rpc_proxy_for(core, "billing.psp.v1", client)
            with pytest.raises(HTTPException) as exc:
                await remote.create_checkout_session()
        # The provider answers 404 "not registered"; the proxy reports it as
        # not deployed (501), exactly like any unknown contract.
        assert exc.value.status_code == 501, exc.value.detail
        assert "not registered" in str(exc.value.detail)
        # Billing's own endpoints still have it.
        assert isinstance(active_psp_client(), StripeClientPort)

    async def test_entitlements_contract(self, test_db: AsyncSession):
        from sandloom_billing.entitlements import EntitlementsService

        user_id = uuid.uuid4()
        assert (
            await EntitlementsService.check_subscription_active(
                "user", user_id, test_db
            )
            is False
        )
        assert (
            await EntitlementsService.get_credit_balance("user", user_id, test_db) == 0
        )

        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await BillingService.add_credits(customer, 7, "ent-grant", test_db)
        assert (
            await EntitlementsService.get_credit_balance("user", user_id, test_db) == 7
        )


@pytest.mark.asyncio
async def test_credits_gate_denies_a_negative_balance_with_402(
    async_client: AsyncClient, test_db: AsyncSession
) -> None:
    """A debt (negative balance, ADR-0003 item 12) is just "not enough
    credits": the usual 402, never an error."""
    user_id, headers = make_user_headers()
    customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
    await BillingService.add_credits(customer, 10, "seed", test_db)
    await BillingService.reverse_credits(customer, 40, "reversal:t", test_db)
    await test_db.commit()

    resp = await async_client.get("/api/v1/_billing_guard_demo/spendy", headers=headers)

    assert resp.status_code == 402, resp.text
