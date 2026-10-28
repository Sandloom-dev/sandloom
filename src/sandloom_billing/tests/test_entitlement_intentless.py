"""Entitlement policy: syncing the PSP's truthful state is NOT granting.

ADR-0003 open items 9 and 11.

- **Unknown price (11, fail closed).** A subscription whose PSP price maps to
  no catalog price grants NOTHING. The webhook is not rejected and the row is
  not deleted: the PSP state (status, period) is stored, the event is
  processed, an audit record flags it for an operator. A change from a known
  price to an unknown one WITHDRAWS the previous entitlements.
- **Intent-less subscriptions (9).** A subscription created without an
  operation intent (operator-created in the PSP dashboard, migrated,
  restored) is stored and synced, but grants entitlements only when it maps to
  a known price AND ``BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`` allows it
  (default ``false``). Intent-backed subscriptions and rows that existed before
  this policy (``origin='legacy'``) are unaffected by the flag.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingSubscription
from sandloom_billing.services import BillingService
from sandloom_billing.tests.conftest import make_user_headers
from sandloom_billing.tests.test_operation_intents import (
    CHECKOUT,
    WEBHOOK,
    _idem,
    _intents,
    _psp_customer,
    _signed,
    sessions,  # noqa: F401 - fixture
)
from sandloom_billing.tests.test_webhook_hardening import (
    NOW,
    _customer,
    _post,
    _sub_event,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def intentless(monkeypatch: pytest.MonkeyPatch) -> Any:
    from sandloom.sdk.infra import settings

    def _set(allow: bool) -> None:
        if "billing_grant_intentless_subscriptions" in type(settings).model_fields:
            monkeypatch.setattr(
                settings, "billing_grant_intentless_subscriptions", allow
            )

    return _set


async def _sub(test_db: AsyncSession, sub_id: str = "sub_h1") -> BillingSubscription:
    result = await test_db.execute(
        select(BillingSubscription)
        .where(BillingSubscription.stripe_subscription_id == sub_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


def _with_price(event: dict, stripe_price: str) -> dict:
    event["data"]["object"]["items"] = {"data": [{"price": {"id": stripe_price}}]}
    return event


async def _audits(db: AsyncSession, action: str) -> int:
    count = await db.execute(
        text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
    )
    return int(count.scalar_one())


# --------------------------------------------------------------------------- #
# Item 9: intent-less subscriptions need an explicit policy
# --------------------------------------------------------------------------- #


async def test_intentless_subscription_is_synced_but_grants_nothing_by_default(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any
) -> None:
    customer = await _customer(test_db)
    resp = await _post(
        async_client, _sub_event("customer.subscription.created", created=NOW)
    )
    assert resp.json()["status"] == "processed", resp.text
    sub = await _sub(test_db)
    assert sub.status == "active" and sub.origin == "psp"
    assert not await BillingService.check_subscription_active(customer.id, test_db)


async def test_intentless_subscription_grants_when_the_policy_allows(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any, intentless: Any
) -> None:
    intentless(True)
    customer = await _customer(test_db)
    await _post(async_client, _sub_event("customer.subscription.created", created=NOW))
    assert await BillingService.check_subscription_active(customer.id, test_db)


async def test_intent_backed_subscription_grants_without_the_policy(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: Any,
    sessions: Any,  # noqa: F811
) -> None:
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [intent] = await _intents(sessions)
    event = _sub_event(
        "customer.subscription.created",
        created=int(time.time()),
        sub_id="sub_intent_1",
        customer=await _psp_customer(sessions),
    )
    event["data"]["object"]["metadata"] = {"sandloom_intent_id": str(intent.id)}
    body, sig = _signed(event)
    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "processed", resp.text
    sub = await _sub(test_db, "sub_intent_1")
    assert sub.origin == "intent"
    assert await BillingService.check_subscription_active(intent.customer_id, test_db)


async def test_an_insert_without_origin_does_not_grant(
    test_db: AsyncSession, catalog: Any
) -> None:
    """Fail closed: a row inserted without ``origin`` (old code during a
    rolling deploy, a manual INSERT, future code) is ``psp`` — it grants
    nothing unless the intent-less policy allows it. Both the ORM default and
    the column's server default."""
    period_end = datetime.now(UTC) + timedelta(days=30)
    orm_customer = await BillingService.get_or_create_customer(
        test_db, user_id=uuid.uuid4()
    )
    test_db.add(
        BillingSubscription(
            customer_id=orm_customer.id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex[:8]}",
            status="active",
            price_id=catalog["pro_month"].id,
            current_period_end=period_end,
        )
    )
    raw_customer = await BillingService.get_or_create_customer(
        test_db, user_id=uuid.uuid4()
    )
    await test_db.execute(
        text(
            "INSERT INTO billing_subscriptions (customer_id, stripe_subscription_id,"
            " status, price_id, current_period_end, cancel_at_period_end)"
            " VALUES (:c, :s, 'active', :p, :e, false)"
        ),
        {
            "c": raw_customer.id,
            "s": f"sub_{uuid.uuid4().hex[:8]}",
            "p": catalog["pro_month"].id,
            "e": period_end,
        },
    )
    await test_db.flush()

    origins = (
        await test_db.execute(
            text(
                "SELECT origin FROM billing_subscriptions WHERE customer_id IN (:a, :b)"
            ),
            {"a": orm_customer.id, "b": raw_customer.id},
        )
    ).scalars()
    assert set(origins) == {"psp"}
    for customer in (orm_customer, raw_customer):
        assert not await BillingService.check_subscription_active(customer.id, test_db)


async def test_rows_from_before_the_policy_keep_granting(
    test_db: AsyncSession, catalog: Any
) -> None:
    """Rows that existed before the policy are backfilled ``origin='legacy'``
    by migration 0005 and keep granting (still with a known price)."""
    customer = await BillingService.get_or_create_customer(
        test_db, user_id=uuid.uuid4()
    )
    test_db.add(
        BillingSubscription(
            customer_id=customer.id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex[:8]}",
            status="active",
            price_id=catalog["pro_month"].id,
            origin="legacy",
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
    )
    await test_db.flush()
    assert await BillingService.check_subscription_active(customer.id, test_db)


async def test_subscription_events_carry_whether_the_subscription_grants(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any, intentless: Any
) -> None:
    """``billing.subscription.*.v1`` carry ``data.grants_access`` (additive in
    .v1): consumers must use it, never derive entitlements from ``status``."""
    await _customer(test_db)
    await _post(async_client, _sub_event("customer.subscription.created", created=NOW))
    intentless(True)
    await _post(
        async_client, _sub_event("customer.subscription.updated", created=NOW + 1)
    )
    rows = (
        await test_db.execute(
            text(
                "SELECT channel, payload FROM outbox_messages WHERE channel LIKE "
                "'billing.subscription.%' ORDER BY emitted_at"
            )
        )
    ).all()
    assert [r.channel for r in rows] == [
        "billing.subscription.created.v1",
        "billing.subscription.updated.v1",
    ]
    assert [r.payload["data"]["status"] for r in rows] == ["active", "active"]
    assert [r.payload["data"]["grants_access"] for r in rows] == [False, True]
    # ... and the catalog plan (additive in .v1), so a consumer projects it
    plans = [r.payload["data"]["plan"] for r in rows]
    assert all(p is not None and p["product"] and "interval" in p for p in plans)
