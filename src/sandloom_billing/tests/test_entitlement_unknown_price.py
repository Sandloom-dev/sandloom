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

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingSubscription
from sandloom_billing.services import BillingService
from sandloom_billing.tests.test_operation_intents import (
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
# Item 11: unknown price -> no entitlements (fail closed)
# --------------------------------------------------------------------------- #


async def test_change_to_an_unknown_price_withdraws_the_entitlements(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any, intentless: Any
) -> None:
    intentless(True)  # isolate the price rule from the intent policy
    customer = await _customer(test_db)
    await _post(
        async_client, _sub_event("customer.subscription.created", created=NOW - 100)
    )
    assert await BillingService.check_subscription_active(customer.id, test_db)

    resp = await _post(
        async_client,
        _with_price(
            _sub_event("customer.subscription.updated", created=NOW),
            "price_not_in_catalog",
        ),
    )

    assert resp.json()["status"] == "processed", resp.text  # not rejected
    sub = await _sub(test_db)
    assert sub.status == "active"  # the PSP's truthful state is stored
    assert sub.price_id is None  # the previous (known) price is NOT retained
    assert not await BillingService.check_subscription_active(customer.id, test_db)
    claims = await BillingService.subscription_claims(customer.id, test_db)
    assert "plan_code" not in claims
    assert await _audits(test_db, "billing.subscription.unknown_price") == 1


async def test_new_subscription_with_an_unknown_price_grants_nothing(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any, intentless: Any
) -> None:
    intentless(True)
    customer = await _customer(test_db)
    resp = await _post(
        async_client,
        _with_price(
            _sub_event("customer.subscription.created", created=NOW),
            "price_not_in_catalog",
        ),
    )
    assert resp.json()["status"] == "processed", resp.text
    assert (await _sub(test_db)).status == "active"
    assert not await BillingService.check_subscription_active(customer.id, test_db)
    assert await _audits(test_db, "billing.subscription.unknown_price") == 1
