"""ADR-0003 open item 16: a checkout intent authorizes ONE subscription to
ITS price.

The intent gate already requires an ``issued`` checkout intent of the event's
customer. A new subscription must also be to the price the intent was
authorized for, and one intent yields at most one subscription. A mismatch is
handled like every other unauthorized completion: not applied (no row),
journaled ``failed``, audited ``billing.intent.unauthorized_completion``,
intent flagged ``followup_at``.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom_billing.models import (
    INTENT_ISSUED,
    BillingPrice,
    BillingSubscription,
    OperationIntent,
)
from sandloom_billing.tests.conftest import make_user_headers
from sandloom_billing.tests.test_operation_intents import (
    CHECKOUT,
    WEBHOOK,
    _audit_in,
    _count_in,
    _idem,
    _intents,
    _psp_customer,
    _signed,
    _subscription_created,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _issued(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> Any:
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED
    return intent


def _with_price(event: dict, stripe_price_id: str) -> dict:
    event["data"]["object"]["items"] = {"data": [{"price": {"id": stripe_price_id}}]}
    return event


async def test_subscription_to_another_price_than_its_intent_is_not_activated(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The intent authorized ``pro_month``; the PSP reports a subscription to
    another (known, more expensive) catalog price: not activated."""
    test_db.add(
        BillingPrice(
            product_id=catalog["pro"].id,
            amount=99900,
            currency="USD",
            recurring_interval="year",
            stripe_price_id="price_pro_enterprise",
        )
    )
    await test_db.commit()
    intent = await _issued(async_client, catalog, sessions)
    event = _with_price(
        _subscription_created(await _psp_customer(sessions), str(intent.id)),
        "price_pro_enterprise",
    )
    body, sig = _signed(event)

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "failed", resp.text
    assert await _count_in(test_db, BillingSubscription) == 0
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1
    flagged = await test_db.get(OperationIntent, intent.id, populate_existing=True)
    assert flagged is not None and flagged.followup_at is not None
    assert flagged.status == INTENT_ISSUED  # never rewritten


async def test_subscription_without_items_is_not_matched_to_an_intent(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """No price to compare: the intent cannot be shown to authorize it."""
    intent = await _issued(async_client, catalog, sessions)
    event = _subscription_created(await _psp_customer(sessions), str(intent.id))
    del event["data"]["object"]["items"]
    body, sig = _signed(event)

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "failed", resp.text
    assert await _count_in(test_db, BillingSubscription) == 0


async def test_one_intent_yields_at_most_one_subscription(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent = await _issued(async_client, catalog, sessions)
    customer = await _psp_customer(sessions)
    first, sig1 = _signed(_subscription_created(customer, str(intent.id)))
    assert (await async_client.post(WEBHOOK, content=first, headers=sig1)).json()[
        "status"
    ] == "processed"

    second, sig2 = _signed(
        _subscription_created(customer, str(intent.id), f"sub_{uuid.uuid4().hex[:8]}")
    )
    resp = await async_client.post(WEBHOOK, content=second, headers=sig2)

    assert resp.json()["status"] == "failed", resp.text
    rows = (await test_db.execute(select(BillingSubscription))).scalars().all()
    assert len(rows) == 1 and rows[0].intent_id == intent.id
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1


async def test_the_matching_subscription_records_its_intent(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent = await _issued(async_client, catalog, sessions)
    body, sig = _signed(
        _subscription_created(await _psp_customer(sessions), str(intent.id))
    )

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "processed", resp.text
    [sub] = (await test_db.execute(select(BillingSubscription))).scalars().all()
    assert sub.intent_id == intent.id
    assert sub.price_id == catalog["pro_month"].id


async def test_credit_completion_for_another_price_than_its_intent_is_refused(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The session names an issued intent and its own session, but another
    (bigger) credit pack than the intent was authorized for."""
    import time

    from sandloom_billing.services import BillingService

    big = BillingPrice(
        product_id=catalog["credits_pack"].id,
        amount=9900,
        currency="USD",
        recurring_interval=None,
        stripe_price_id="price_credits_1000",
        credits=1000,
    )
    test_db.add(big)
    await test_db.commit()
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pack_price"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [session] = fake_psp.checkout_sessions
    payload = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": session["id"],
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": big.amount,
                "currency": "usd",
                "customer": session["customer"],
                "metadata": {**session["metadata"], "price_id": str(big.id)},
            }
        },
    }
    body, sig = _signed(payload)

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "failed", resp.text
    [intent] = await _intents(sessions)
    assert await BillingService.get_credit_balance(intent.customer_id, test_db) == 0
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1


async def test_subscription_with_a_quantity_above_one_is_not_activated(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Billing creates checkout line items with quantity 1: three seats of the
    intent's price are not what the intent authorized."""
    intent = await _issued(async_client, catalog, sessions)
    event = _subscription_created(await _psp_customer(sessions), str(intent.id))
    event["data"]["object"]["items"] = {
        "data": [{"price": {"id": "price_pro_month"}, "quantity": 3}]
    }
    body, sig = _signed(event)

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "failed", resp.text
    assert await _count_in(test_db, BillingSubscription) == 0
