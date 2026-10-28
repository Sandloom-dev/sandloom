"""Invariants of the refund / dispute policy and negative balances (ADR-0003
item 12), pinned with REAL concurrent transactions where it matters:

- balance == SUM(ledger.delta) always; the balance may go negative (debt),
  presented by the API as an explicit debt; spending stays atomic and is
  refused while the balance is short; ``Credits(n)`` keeps answering 402;
- (1) a payment's state is never applied to another customer's grant;
- (2) reordering never lets an older state overwrite a newer one: the state is
  decided by monotone fields and terminal statuses, not by delivery order;
- (3) concurrent webhooks never double-reverse, double-restore or deadlock.
"""

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import BigInteger, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.testing import committing_uow
from sandloom_billing.exceptions import InsufficientCreditsError
from sandloom_billing.models import (
    BillingCustomer,
    CreditBalance,
    CreditLedgerEntry,
    PspPaymentState,
)
from sandloom_billing.services import BillingService
from sandloom_billing.services.webhook_service import CHECKOUT_COMPLETED, WebhookService
from sandloom_billing.tests.test_refunds_disputes import (
    _balance,
    _bought_pack,
    _dispute,
    _post,
    _refund,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _ledger_sum(db: AsyncSession, customer_id: uuid.UUID) -> int:
    total = await db.execute(
        select(func.coalesce(func.sum(CreditLedgerEntry.delta), 0)).where(
            CreditLedgerEntry.customer_id == customer_id
        )
    )
    return int(total.scalar_one())


# --------------------------------------------------------------------------- #
# Negative balance = explicit debt
# --------------------------------------------------------------------------- #


def test_balance_and_ledger_columns_are_bigint() -> None:
    """Overflow bound: BIGINT (9.2e18) vs at most 1e12 credits per operation
    (``MAX_CREDIT_OPERATION``); an overflow would fail the transaction, never
    wrap."""
    assert isinstance(CreditBalance.__table__.c.amount.type, BigInteger)
    assert isinstance(CreditLedgerEntry.__table__.c.delta.type, BigInteger)
    assert isinstance(CreditLedgerEntry.__table__.c.balance_after.type, BigInteger)


async def test_negative_balance_is_presented_as_a_debt(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    owner = await test_db.get(BillingCustomer, intent.customer_id)
    assert owner is not None
    await BillingService.spend_credits(owner, 60, "use-60", test_db)
    await test_db.commit()
    await _post(async_client, _refund(customer, 1900))

    from sandloom.sdk.testing import as_user

    _, headers = as_user(
        user_id=intent.actor_id, tenant=False, email=None, email_verified=None
    )
    resp = await async_client.get("/api/v1/billing/credits", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"balance": -60, "debt": 60, "in_debt": True}
    assert await _ledger_sum(test_db, intent.customer_id) == -60


async def test_spends_from_a_negative_balance_are_refused_atomically(
    test_engine: AsyncEngine,
) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    async with committing_uow(maker) as s:
        customer = await BillingService.get_or_create_customer(s, user_id=user_id)
        await BillingService.add_credits(customer, 10, "seed", s)
        await BillingService.reverse_credits(customer, 40, "reversal:x", s)
        customer_id = customer.id

    async def _spend(i: int) -> int:
        async with committing_uow(maker) as s:
            c = await BillingService.get_or_create_customer(s, user_id=user_id)
            return await BillingService.spend_credits(c, 1, f"spend-{i}", s)

    results = await asyncio.gather(
        *(_spend(i) for i in range(5)), return_exceptions=True
    )

    assert all(isinstance(r, InsufficientCreditsError) for r in results), results
    async with maker() as s:
        assert await BillingService.get_credit_balance(customer_id, s) == -30
        assert await _ledger_sum(s, customer_id) == -30


# --------------------------------------------------------------------------- #
# (2) Reordering: monotone state, terminal disputes
# --------------------------------------------------------------------------- #


async def test_won_delivered_before_created_and_updated_reverses_nothing(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)

    await _post(async_client, _dispute("charge.dispute.closed", "won"))
    await _post(async_client, _dispute("charge.dispute.created", "needs_response"))
    await _post(async_client, _dispute("charge.dispute.updated", "under_review"))

    assert await _balance(test_db, intent) == 100


async def test_late_needs_response_after_won_does_not_reopen(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    await _post(async_client, _dispute("charge.dispute.created", "needs_response"))
    await _post(async_client, _dispute("charge.dispute.closed", "won"))

    late = await _post(
        async_client, _dispute("charge.dispute.updated", "needs_response")
    )

    assert late["status"] == "ignored"
    assert await _balance(test_db, intent) == 100
    # And a smaller cumulative refund after a bigger one never lowers it.
    await _post(async_client, _refund(customer, 1900))
    await _post(async_client, _refund(customer, 950))
    assert await _balance(test_db, intent) == 0
    assert await _ledger_sum(test_db, intent.customer_id) == 0


# --------------------------------------------------------------------------- #
# (1) + (3) with real concurrent transactions
# --------------------------------------------------------------------------- #


async def _committed_grant(
    maker: async_sessionmaker[AsyncSession], catalog: dict[str, Any], pi: str
) -> tuple[uuid.UUID, str]:
    external = f"cus_inv_{uuid.uuid4().hex[:8]}"
    async with committing_uow(maker) as s:
        customer = await BillingService.get_or_create_customer(s, user_id=uuid.uuid4())
        customer.stripe_customer_id = external
        customer_id = customer.id
    pack = catalog["pack_price"]
    async with committing_uow(maker) as s:
        await WebhookService._apply_checkout(
            CHECKOUT_COMPLETED,
            {
                "id": f"cs_inv_{uuid.uuid4().hex[:8]}",
                "mode": "payment",
                "payment_status": "paid",
                "payment_intent": pi,
                "amount_total": pack.amount,
                "currency": "usd",
                "customer": external,
                "metadata": {
                    "customer_id": str(customer_id),
                    "price_id": str(pack.id),
                },
            },
            s,
            event_id=f"evt_grant_{uuid.uuid4().hex[:6]}",
        )
    return customer_id, external


def _with_pi(event: dict, pi: str) -> dict:
    event["data"]["object"]["payment_intent"] = pi
    return event


def _refund_call(event: dict) -> Any:
    return lambda s: WebhookService._apply_charge_refunded(
        event, event["data"]["object"], s
    )


def _dispute_call(event_type: str, event: dict) -> Any:
    return lambda s: WebhookService._apply_dispute(
        event_type, event, event["data"]["object"], s
    )


async def _concurrently(
    maker: Any, *calls: Any, barrier_pi: str | None = None
) -> list[Any]:
    """Run each call in its own committing transaction, concurrently.

    ``barrier_pi``: a third transaction holds that payment's state row lock
    while every call starts; the test asserts they are all blocked on it (so
    they deterministically overlap), then releases it."""

    async def _one(call: Any) -> Any:
        async with committing_uow(maker) as s:
            return await call(s)

    if barrier_pi is None:
        return await asyncio.wait_for(
            asyncio.gather(*(_one(c) for c in calls), return_exceptions=True), 20
        )
    async with maker() as holder:
        await holder.execute(
            select(PspPaymentState)
            .where(PspPaymentState.psp_payment_intent_id == barrier_pi)
            .with_for_update()
        )
        tasks = [asyncio.create_task(_one(c)) for c in calls]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in tasks), "calls did not overlap"
        await holder.rollback()  # release: all of them race for the row now
    return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)


async def test_foreign_payment_state_is_never_applied_to_another_grant(
    test_engine: AsyncEngine, catalog: dict[str, Any]
) -> None:
    """(1) Customer A's refund names A's payment; customer B's grant (another
    payment) is untouched — and a refund naming B's payment but A's PSP
    customer is refused."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    a_id, a_ext = await _committed_grant(maker, catalog, "pi_inv_a")
    b_id, _ = await _committed_grant(maker, catalog, "pi_inv_b")
    a_refund = _with_pi(_refund(a_ext, 1900), "pi_inv_a")
    forged = _with_pi(_refund(a_ext, 1900), "pi_inv_b")

    results = await _concurrently(
        maker,
        lambda s: WebhookService._apply_charge_refunded(
            a_refund, a_refund["data"]["object"], s
        ),
        lambda s: WebhookService._apply_charge_refunded(
            forged, forged["data"]["object"], s
        ),
    )

    from sandloom_billing.services.webhook_service import WebhookDataError

    forged_errors = [r for r in results if isinstance(r, Exception)]
    assert len(forged_errors) == 1, results
    assert isinstance(forged_errors[0], WebhookDataError), forged_errors
    assert "not the grant's" in str(forged_errors[0])
    async with maker() as s:
        assert await BillingService.get_credit_balance(a_id, s) == 0
        assert await BillingService.get_credit_balance(b_id, s) == 100


async def test_concurrent_refund_deliveries_reverse_once(
    test_engine: AsyncEngine, catalog: dict[str, Any]
) -> None:
    """(3) Two deliveries of the same refund state (different event ids) in
    two transactions at once: one reversal, no deadlock."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    customer_id, external = await _committed_grant(maker, catalog, "pi_inv_r")
    events = [_with_pi(_refund(external, 1900), "pi_inv_r") for _ in range(2)]

    results = await _concurrently(
        maker,
        *(_refund_call(e) for e in events),
        barrier_pi="pi_inv_r",
    )

    assert not any(isinstance(r, Exception) for r in results), results
    async with maker() as s:
        assert await BillingService.get_credit_balance(customer_id, s) == 0
        assert await _ledger_sum(s, customer_id) == 0
        reversals = await s.execute(
            select(func.count()).where(
                CreditLedgerEntry.customer_id == customer_id,
                CreditLedgerEntry.delta < 0,
            )
        )
        assert int(reversals.scalar_one()) == 1


async def test_concurrent_won_deliveries_restore_once(
    test_engine: AsyncEngine, catalog: dict[str, Any]
) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    customer_id, _ = await _committed_grant(maker, catalog, "pi_inv_w")
    opened = _with_pi(_dispute("charge.dispute.created", "needs_response"), "pi_inv_w")
    async with committing_uow(maker) as s:
        await WebhookService._apply_dispute(
            "charge.dispute.created", opened, opened["data"]["object"], s
        )
    wins = [
        _with_pi(_dispute("charge.dispute.closed", "won"), "pi_inv_w") for _ in range(2)
    ]

    results = await _concurrently(
        maker,
        *(_dispute_call("charge.dispute.closed", e) for e in wins),
        barrier_pi="pi_inv_w",
    )

    assert not any(isinstance(r, Exception) for r in results), results
    async with maker() as s:
        assert await BillingService.get_credit_balance(customer_id, s) == 100
        assert await _ledger_sum(s, customer_id) == 100


async def test_concurrent_refund_and_dispute_converge_without_deadlock(
    test_engine: AsyncEngine, catalog: dict[str, Any]
) -> None:
    """(3) A half refund and a dispute of the other half at once: same lock
    order (state -> grant -> ledger) in both, so no deadlock; the total is
    capped at the grant."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    customer_id, external = await _committed_grant(maker, catalog, "pi_inv_rd")
    refund = _with_pi(_refund(external, 950), "pi_inv_rd")
    dispute = _with_pi(
        _dispute("charge.dispute.created", "needs_response", amount=950),
        "pi_inv_rd",
    )

    results = await _concurrently(
        maker,
        lambda s: WebhookService._apply_charge_refunded(
            refund, refund["data"]["object"], s
        ),
        lambda s: WebhookService._apply_dispute(
            "charge.dispute.created", dispute, dispute["data"]["object"], s
        ),
        barrier_pi="pi_inv_rd",
    )

    assert not any(isinstance(r, Exception) for r in results), results
    async with maker() as s:
        assert await BillingService.get_credit_balance(customer_id, s) == 0
        assert await _ledger_sum(s, customer_id) == 0
