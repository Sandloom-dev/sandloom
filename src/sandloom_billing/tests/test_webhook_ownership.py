"""ADR-0003 webhook matrix: PSP lifecycle events verify ownership and only
apply valid transitions.

Gaps closed here (each test failed before its fix):
- ``invoice.paid`` recovered a subscription found by id alone: the invoice's
  PSP customer must own that subscription;
- a subscription event carried any status string into the row: only Stripe's
  subscription statuses apply, ``incomplete_expired`` is terminal like
  ``canceled``.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingSubscription
from sandloom_billing.tests.test_webhook_hardening import (
    NOW,
    _customer,
    _invoice_event,
    _post,
    _sub_event,
)

pytestmark = pytest.mark.asyncio


async def _sub(test_db: AsyncSession, sub_id: str = "sub_h1") -> BillingSubscription:
    result = await test_db.execute(
        select(BillingSubscription)
        .where(BillingSubscription.stripe_subscription_id == sub_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def test_invoice_paid_of_another_customer_does_not_recover_the_subscription(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any
) -> None:
    await _customer(test_db, "cus_h")
    await _customer(test_db, "cus_other")
    await _post(
        async_client,
        _sub_event(
            "customer.subscription.created", created=NOW - 100, status="past_due"
        ),
    )
    invoice = _invoice_event(created=NOW, line_end=NOW + 86400 * 30)
    invoice["data"]["object"]["customer"] = "cus_other"  # not the owner

    resp = await _post(async_client, invoice)

    assert resp.json()["status"] == "failed", resp.text
    assert (await _sub(test_db)).status == "past_due"


async def test_invoice_paid_of_the_owner_still_recovers(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any
) -> None:
    await _customer(test_db, "cus_h")
    await _post(
        async_client,
        _sub_event(
            "customer.subscription.created", created=NOW - 100, status="past_due"
        ),
    )
    resp = await _post(
        async_client, _invoice_event(created=NOW, line_end=NOW + 86400 * 30)
    )
    assert resp.json()["status"] == "processed", resp.text
    assert (await _sub(test_db)).status == "active"


async def test_unknown_subscription_status_is_not_applied(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any
) -> None:
    await _customer(test_db, "cus_h")
    await _post(
        async_client, _sub_event("customer.subscription.created", created=NOW - 100)
    )
    resp = await _post(
        async_client,
        _sub_event("customer.subscription.updated", created=NOW, status="superactive"),
    )
    assert resp.json()["status"] == "failed", resp.text
    assert (await _sub(test_db)).status == "active"


async def test_incomplete_expired_is_terminal(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any
) -> None:
    """Stripe never revives an ``incomplete_expired`` subscription; a later
    (replayed / forged) ``active`` must not either."""
    await _customer(test_db, "cus_h")
    sub_id = f"sub_{uuid.uuid4().hex[:8]}"
    await _post(
        async_client,
        _sub_event(
            "customer.subscription.created",
            created=NOW - 100,
            status="incomplete",
            sub_id=sub_id,
        ),
    )
    await _post(
        async_client,
        _sub_event(
            "customer.subscription.updated",
            created=NOW - 50,
            status="incomplete_expired",
            sub_id=sub_id,
        ),
    )
    resp = await _post(
        async_client,
        _sub_event(
            "customer.subscription.updated", created=NOW, status="active", sub_id=sub_id
        ),
    )
    assert resp.json()["status"] == "ignored", resp.text
    assert (await _sub(test_db, sub_id)).status == "incomplete_expired"


async def test_subscription_of_another_customer_is_not_taken_over(
    async_client: AsyncClient, test_db: AsyncSession, catalog: Any
) -> None:
    """Existing check, pinned: an event moving a known subscription to
    another PSP customer is refused."""
    await _customer(test_db, "cus_h")
    await _customer(test_db, "cus_other")
    await _post(
        async_client, _sub_event("customer.subscription.created", created=NOW - 100)
    )
    resp = await _post(
        async_client,
        _sub_event(
            "customer.subscription.updated",
            created=NOW,
            status="canceled",
            customer="cus_other",
        ),
    )
    assert resp.json()["status"] == "failed", resp.text
    assert (await _sub(test_db)).status == "active"
