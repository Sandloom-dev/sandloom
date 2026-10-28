"""Portal-driven subscription changes (ADR-0003 open item 10).

Stripe does not link a change made in the customer portal to the portal
session, so billing cannot GATE such changes on a portal intent (blocking a
lifecycle update would desynchronize billing from the PSP's truth). What it
does:

- the change is applied only for the customer that owns the subscription and
  is evaluated by the entitlement policy (known price; origin rules) —
  already the case for every update;
- a user-actionable change (price / plan, ``cancel_at_period_end``) for a
  customer with NO issued portal intent in the preceding attribution window
  is audited ``billing.subscription.unattributed_change`` — never blocked.
"""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom_billing.models import BillingPrice, OperationIntent
from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.tests.conftest import WEBHOOK_SECRET, make_user_headers
from sandloom_billing.tests.test_operation_intents import (
    CHECKOUT,
    PORTAL,
    WEBHOOK,
    _idem,
    _intents,
    _psp_customer,
    _subscription_created,
)

pytestmark = pytest.mark.asyncio

AUDIT = "billing.subscription.unattributed_change"


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _post(client: AsyncClient, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    resp = await client.post(
        WEBHOOK,
        content=body,
        headers={"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _audits(db: AsyncSession) -> int:
    count = await db.execute(
        text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": AUDIT}
    )
    return int(count.scalar_one())


async def _subscribed(
    client: AsyncClient,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[dict, str, dict]:
    """An intent-backed active subscription; returns (headers, sub id, obj)."""
    _, headers = make_user_headers()
    resp = await client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [intent] = await _intents(sessions)
    created = _subscription_created(await _psp_customer(sessions), str(intent.id))
    created["created"] = int(time.time()) - 60
    assert (await _post(client, created))["status"] == "processed"
    return headers, created["data"]["object"]["id"], created["data"]["object"]


def _updated(obj: dict, **changes: Any) -> dict:
    return {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "customer.subscription.updated",
        "created": int(time.time()),
        "data": {"object": {**obj, **changes}},
    }


async def _open_portal(client: AsyncClient, headers: dict) -> None:
    resp = await client.post(PORTAL, json={}, headers=_idem(headers))
    assert resp.status_code == 200, resp.text


async def test_cancel_at_period_end_without_a_portal_intent_is_audited_not_blocked(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, sub_id, obj = await _subscribed(async_client, catalog, sessions)

    result = await _post(async_client, _updated(obj, cancel_at_period_end=True))

    assert result["status"] == "processed", result
    sub = (
        await test_db.execute(
            text(
                "SELECT cancel_at_period_end FROM billing_subscriptions "
                "WHERE stripe_subscription_id = :s"
            ),
            {"s": sub_id},
        )
    ).scalar_one()
    assert sub is True  # applied: the PSP's state is the truth
    assert await _audits(test_db) == 1


async def test_change_after_an_issued_portal_intent_is_attributed(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    headers, _, obj = await _subscribed(async_client, catalog, sessions)
    await _open_portal(async_client, headers)

    result = await _post(async_client, _updated(obj, cancel_at_period_end=True))

    assert result["status"] == "processed", result
    assert await _audits(test_db) == 0


async def test_plan_change_long_after_the_portal_intent_is_audited(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    test_db.add(
        BillingPrice(
            product_id=catalog["pro"].id,
            amount=99000,
            currency="USD",
            recurring_interval="year",
            stripe_price_id="price_pro_year",
        )
    )
    await test_db.commit()
    headers, _, obj = await _subscribed(async_client, catalog, sessions)
    await _open_portal(async_client, headers)
    async with sessions() as db:
        await db.execute(
            update(OperationIntent)
            .where(OperationIntent.operation == "customer_portal")
            .values(issued_at=datetime.now(UTC) - timedelta(hours=5))
        )
        await db.commit()

    result = await _post(
        async_client,
        _updated(obj, items={"data": [{"price": {"id": "price_pro_year"}}]}),
    )

    assert result["status"] == "processed", result
    assert await _audits(test_db) == 1


async def test_lifecycle_update_is_never_audited(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A renewal / dunning update (status, period) is the PSP's lifecycle, not
    a customer action."""
    _, _, obj = await _subscribed(async_client, catalog, sessions)

    result = await _post(
        async_client,
        _updated(
            obj,
            status="past_due",
            current_period_end=int(time.time()) + 86400 * 60,
        ),
    )

    assert result["status"] == "processed", result
    assert await _audits(test_db) == 0


async def test_another_customers_portal_intent_does_not_attribute(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Owner-scoped: only the subscription's own customer's portal intent
    attributes a change."""
    _, _, obj = await _subscribed(async_client, catalog, sessions)
    _, stranger = make_user_headers()
    await _open_portal(async_client, stranger)

    await _post(async_client, _updated(obj, cancel_at_period_end=True))

    assert await _audits(test_db) == 1
    count = await test_db.execute(text("SELECT count(*) FROM billing_subscriptions"))
    assert int(count.scalar_one()) == 1
