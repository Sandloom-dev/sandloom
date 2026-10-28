"""Billing's declared reads (``GET /subscription``, ``GET /credits``).

* every operation is planned on the OPTIMIZED executor when it is enabled
  (no silent ORM fallback), and on the general one when it is not;
* the balance read keyed only on the customer's tenant column (``by=()``) is
  a JoinRead the planner proves single-row: ``credit_balances.customer_id``
  is UNIQUE and the customer is pinned by a UNIQUE tenant column (the
  refusal of a multi-row join is pinned in the kernel's planner tests);
* both executors answer the same DTOs for the same rows (subscriptions newest
  first, the plan of a price, the balance), and nothing for a tenant without
  a customer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.data import session_reads
from sandloom.sdk.infra import settings
from sandloom_billing.models import BillingSubscription
from sandloom_billing.services import BillingService
from sandloom_billing.services.operations import (
    OPERATIONS,
    balance_of,
    plan_of_price,
    subscriptions_of,
)

pytestmark = pytest.mark.asyncio


TOKEN = "billing-planner-diag-internal-token-0123456789ab"


async def _run_prepare_tasks(facade: Any) -> None:
    for task, critical in facade.startup_task_specs:
        if getattr(task, "__name__", "") == "prepare_declared_operations":
            assert critical
            try:
                await task()
            except Exception:  # noqa: BLE001, S112 - another module's bad declaration may fail it
                # another module's test may have registered a deliberately
                # bad declaration on the shared facade: billing's own task
                # is checked through the diagnostics below
                continue


@pytest.mark.parametrize("optimized", [True, False])
async def test_every_operation_has_the_expected_executor(
    sandloom_app: Any,
    sandloom_core: Any,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    optimized: bool,
) -> None:
    """Planned at startup; with the optimized executor on, none of billing's
    reads falls back to the ORM (the balance read keyed on the customer's
    tenant column is proven single-row: both UNIQUE)."""
    monkeypatch.setattr(settings, "planner_optimized_executor", optimized)
    await _run_prepare_tasks(sandloom_core)
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    resp = await client.get("/diagnostics/core", headers={"X-Internal-Token": TOKEN})
    assert resp.status_code == 200, resp.text
    ops = {o["name"]: o for o in resp.json()["planner"]["operations"]}
    for op in OPERATIONS:
        entry = ops[op.name]
        assert entry["module"].startswith("sandloom_billing"), entry
        assert entry["executor"] == ("optimized" if optimized else "general"), entry


async def _seed(test_db: AsyncSession, catalog: dict[str, Any]) -> uuid.UUID:
    user_id = uuid.uuid4()
    customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
    now = datetime.now(UTC)
    test_db.add_all(
        [
            BillingSubscription(
                customer_id=customer.id,
                price_id=catalog["pro_month"].id,
                status="canceled",
                origin="intent",
            ),
            BillingSubscription(
                customer_id=customer.id,
                price_id=catalog["pro_month"].id,
                status="active",
                origin="intent",
                current_period_end=now + timedelta(days=3),
            ),
        ]
    )
    await test_db.flush()
    await BillingService.add_credits(customer, 9, f"g-{user_id}", test_db)
    return user_id


@pytest.mark.parametrize("optimized", [True, False])
async def test_reads_answer_the_rows_on_both_executors(
    test_db: AsyncSession,
    catalog: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    optimized: bool,
) -> None:
    monkeypatch.setattr(settings, "planner_optimized_executor", optimized)
    user_id = await _seed(test_db, catalog)
    reads = session_reads(test_db)  # the test's transaction (general form)
    page = await reads.page(subscriptions_of["user"], limit=10, tenant_id=user_id)
    assert [s.status for s in page.items] == ["active", "canceled"]
    assert page.items[0].id > page.items[1].id and page.next_cursor is None
    plan = await reads.one(plan_of_price, id=catalog["pro_month"].id)
    assert plan is not None and plan.name == "Pro"
    assert plan.product_id == catalog["pro_month"].product_id
    balance = await reads.one(balance_of["user"], tenant_id=user_id)
    assert balance is not None and balance.amount == 9
    # a tenant without a customer: nothing, on every read
    stranger = uuid.uuid4()
    assert (
        await reads.page(subscriptions_of["user"], limit=10, tenant_id=stranger)
    ).items == []
    assert await reads.one(balance_of["user"], tenant_id=stranger) is None
    assert await reads.one(balance_of["organization"], tenant_id=user_id) is None
    status = await BillingService.tenant_subscription_status("user", user_id, reads)
    assert status.status == "active" and status.plan is not None
    assert await BillingService.tenant_credit_balance("user", user_id, reads) == 9


async def test_subscription_pages_are_all_read(
    test_db: AsyncSession,
    catalog: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More subscriptions than one page: the status still sees every one
    (the current one is the newest GRANTING one, not the newest)."""
    monkeypatch.setattr(settings, "planner_list_max_limit", 2)
    user_id = await _seed(test_db, catalog)
    customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
    test_db.add_all(
        [
            BillingSubscription(
                customer_id=customer.id,
                price_id=catalog["pro_month"].id,
                status="canceled",
                origin="intent",
            )
            for _ in range(3)
        ]
    )
    await test_db.flush()
    reads = session_reads(test_db)
    status = await BillingService.tenant_subscription_status("user", user_id, reads)
    expected = await BillingService.get_subscription_status(customer.id, test_db)
    assert status == expected and status.status == "active"
