"""Who creates a billing customer, now that the GETs are pure reads.

``GET /subscription`` / ``GET /credits`` never write: a tenant without a
customer answers what a freshly created customer answered (pinned field by
field below). The customer is created by the checkout (its operation intent),
and the purchases it leads to — a credit grant, a subscription — land on THAT
customer and show up in the reads.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingCustomer, CreditBalance
from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.tests.conftest import WEBHOOK_SECRET

pytestmark = pytest.mark.asyncio

CHECKOUT = "/api/v1/billing/checkout-session"
WEBHOOK = "/api/v1/billing/webhooks/stripe"
#: What both GETs answered right after creating a customer (main @ f21f617c),
#: and answer now for a tenant without one.
FRESH_SUBSCRIPTION = {
    "status": "none",
    "current_period_end": None,
    "trial_end": None,
    "cancel_at_period_end": False,
    "plan": None,
}
FRESH_CREDITS = {"balance": 0, "debt": 0, "in_debt": False}


async def _count(db: AsyncSession, model: Any, *where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int(await db.scalar(stmt) or 0)


async def _reads(client: AsyncClient, headers: dict[str, str]) -> tuple[Any, Any]:
    sub = await client.get("/api/v1/billing/subscription", headers=headers)
    credits = await client.get("/api/v1/billing/credits", headers=headers)
    assert sub.status_code == credits.status_code == 200, (sub.text, credits.text)
    return sub.json(), credits.json()


def _signed(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)}


async def _checkout(
    client: AsyncClient, headers: dict[str, str], price_id: int, fake_psp: Any
) -> dict[str, Any]:
    resp = await client.post(
        CHECKOUT,
        json={"price_id": price_id},
        headers={**headers, "Idempotency-Key": f"lc-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 200, resp.text
    return dict(fake_psp.checkout_sessions[-1])


async def test_reads_create_nothing_checkout_creates_and_grants_land_on_it(
    async_client: AsyncClient,
    test_db: AsyncSession,
    user_a: tuple[uuid.UUID, dict[str, str]],
    catalog: dict[str, Any],
    fake_psp: Any,
) -> None:
    user_id, headers = user_a
    mine = BillingCustomer.user_id == user_id

    # 1. reads only: the fresh-customer answer, nothing written
    for _ in range(2):
        assert await _reads(async_client, headers) == (
            FRESH_SUBSCRIPTION,
            FRESH_CREDITS,
        )
    assert await _count(test_db, BillingCustomer) == 0
    assert await _count(test_db, CreditBalance) == 0

    # 2. the checkout creates the customer (and its zero balance row)
    pack = await _checkout(async_client, headers, catalog["pack_price"].id, fake_psp)
    assert await _count(test_db, BillingCustomer, mine) == 1
    customer_id = await test_db.scalar(select(BillingCustomer.id).where(mine))
    assert (
        await _count(test_db, CreditBalance, CreditBalance.customer_id == customer_id)
        == 1
    )
    assert await _reads(async_client, headers) == (FRESH_SUBSCRIPTION, FRESH_CREDITS)

    # 3. the paid purchase grants the pack's credits to THAT customer
    body, sig = _signed(
        {
            "id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": pack["id"],
                    "mode": "payment",
                    "payment_status": "paid",
                    "payment_intent": f"pi_{uuid.uuid4().hex[:12]}",
                    "amount_total": catalog["pack_price"].amount,
                    "currency": "usd",
                    "customer": pack["customer"],
                    "metadata": pack["metadata"],
                }
            },
        }
    )
    resp = await async_client.post(WEBHOOK, content=body, headers=sig)
    assert resp.status_code == 200 and resp.json()["status"] == "processed", resp.text
    _, credits = await _reads(async_client, headers)
    assert credits == {"balance": 100, "debt": 0, "in_debt": False}

    # 4. a subscription bought through the checkout shows up on the read
    pro = await _checkout(async_client, headers, catalog["pro_month"].id, fake_psp)
    body, sig = _signed(
        {
            "id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": "customer.subscription.created",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": f"sub_{uuid.uuid4().hex[:12]}",
                    "customer": pro["customer"],
                    "status": "active",
                    "current_period_end": int(time.time()) + 86400 * 30,
                    "cancel_at_period_end": False,
                    "metadata": pro["metadata"],
                    "items": {"data": [{"price": {"id": "price_pro_month"}}]},
                }
            },
        }
    )
    resp = await async_client.post(WEBHOOK, content=body, headers=sig)
    assert resp.status_code == 200 and resp.json()["status"] == "processed", resp.text
    sub, credits = await _reads(async_client, headers)
    assert sub["status"] == "active" and sub["plan"]["name"] == "Pro", sub
    assert credits["balance"] == 100
    assert await _count(test_db, BillingCustomer, mine) == 1  # still one


async def test_org_reads_create_nothing_for_the_org(
    async_client: AsyncClient,
    test_db: AsyncSession,
    user_a: tuple[uuid.UUID, dict[str, str]],
) -> None:
    from sandloom_billing.tests import tenancy_api

    _, headers = user_a
    org_id = await tenancy_api.create_org(async_client, headers)
    for query in ("", f"?org_id={org_id}"):
        sub = await async_client.get(
            f"/api/v1/billing/subscription{query}", headers=headers
        )
        credits = await async_client.get(
            f"/api/v1/billing/credits{query}", headers=headers
        )
        assert (sub.json(), credits.json()) == (FRESH_SUBSCRIPTION, FRESH_CREDITS)
    assert await _count(test_db, BillingCustomer) == 0
