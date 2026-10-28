"""Refunds and disputes of credit-pack payments (ADR-0003 open item 12).

Credits granted from a payment that is refunded or disputed are reversed
through the credits ledger: an append-only NEGATIVE entry per PSP event
(durably deduped by its operation key, in the webhook's transaction), never
an edit of the grant. The PSP customer of a refunded charge must own the
grant. The balance MAY go negative (credits already spent are owed; spending
is refused until purchases cover it). A dispute reverses the remaining
credits at once; a dispute closed ``won`` restores what the dispute reversed.
"""

import json
import time
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom_billing.models import CreditLedgerEntry
from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.services import BillingService
from sandloom_billing.tests.conftest import WEBHOOK_SECRET, make_user_headers
from sandloom_billing.tests.test_operation_intents import (
    CHECKOUT,
    WEBHOOK,
    _idem,
    _intents,
)

pytestmark = pytest.mark.asyncio

PI = "pi_test_pack"


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


def _event(event_type: str, obj: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": obj},
    }


async def _post(client: AsyncClient, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    resp = await client.post(
        WEBHOOK,
        content=body,
        headers={"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _bought_pack(
    client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
    *,
    before_completion: list[Any] | None = None,
) -> tuple[Any, str]:
    """A paid credit-pack checkout (100 credits for 19.00 USD) with ``PI``.
    ``before_completion``: PSP events delivered BEFORE the completion (out of
    order); each is built by ``f(psp_customer)``."""
    _, headers = make_user_headers()
    resp = await client.post(
        CHECKOUT, json={"price_id": catalog["pack_price"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [session] = fake_psp.checkout_sessions
    for early in before_completion or []:
        early_event = early(session["customer"]) if callable(early) else early
        assert (await _post(client, early_event))["status"] == "processed"
    paid = await _post(
        client,
        _event(
            "checkout.session.completed",
            {
                "id": session["id"],
                "mode": "payment",
                "payment_status": "paid",
                "payment_intent": PI,
                "amount_total": catalog["pack_price"].amount,
                "currency": "usd",
                "customer": session["customer"],
                "metadata": session["metadata"],
            },
        ),
    )
    assert paid["status"] == "processed", paid
    [intent] = await _intents(sessions)
    return intent, session["customer"]


def _refund(customer: str, refunded: int, *, amount: int = 1900) -> dict:
    return _event(
        "charge.refunded",
        {
            "id": "ch_test_pack",
            "object": "charge",
            "amount": amount,
            "amount_refunded": refunded,
            "currency": "usd",
            "customer": customer,
            "payment_intent": PI,
            "refunded": refunded >= amount,
        },
    )


def _dispute(
    event_type: str, status: str, dispute_id: str = "dp_1", amount: int = 1900
) -> dict:
    return _event(
        event_type,
        {
            "id": dispute_id,
            "object": "dispute",
            "amount": amount,
            "currency": "usd",
            "charge": "ch_test_pack",
            "payment_intent": PI,
            "status": status,
        },
    )


async def _balance(db: AsyncSession, intent: Any) -> int:
    return await BillingService.get_credit_balance(intent.customer_id, db)


async def test_same_payment_intent_under_another_session_never_double_grants(
    async_client, test_db, catalog, fake_psp, sessions
):
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    metadata = dict(fake_psp.checkout_sessions[0]["metadata"])
    metadata.pop("sandloom_intent_id", None)
    result = await _post(
        async_client,
        _event(
            "checkout.session.completed",
            {
                "id": "cs_same_payment_another_session",
                "mode": "payment",
                "payment_status": "paid",
                "payment_intent": PI,
                "amount_total": catalog["pack_price"].amount,
                "currency": "usd",
                "customer": customer,
                "metadata": metadata,
            },
        ),
    )
    assert result["status"] in {"ignored", "failed"}
    assert await _balance(test_db, intent) == 100


async def _audits(db: AsyncSession, action: str) -> int:
    count = await db.execute(
        text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
    )
    return int(count.scalar_one())


async def test_full_refund_reverses_the_granted_credits(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    assert await _balance(test_db, intent) == 100

    result = await _post(async_client, _refund(customer, 1900))

    assert result["status"] == "processed", result
    assert await _balance(test_db, intent) == 0
    rows = (
        (
            await test_db.execute(
                select(CreditLedgerEntry.delta).order_by(CreditLedgerEntry.id)
            )
        )
        .scalars()
        .all()
    )
    assert list(rows) == [100, -100]  # append-only reversal, grant untouched
    assert await _audits(test_db, "billing.credits.reversed") == 1


async def test_partial_refunds_reverse_proportionally_and_cumulatively(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """``amount_refunded`` is cumulative: half, then all; an older (smaller)
    cumulative refund delivered late reverses nothing more."""
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    half = _refund(customer, 950)

    assert (await _post(async_client, half))["status"] == "processed"
    assert await _balance(test_db, intent) == 50
    assert (await _post(async_client, _refund(customer, 1900)))["status"] == (
        "processed"
    )
    assert await _balance(test_db, intent) == 0
    late = _refund(customer, 950)
    assert (await _post(async_client, late))["status"] == "ignored"
    assert await _balance(test_db, intent) == 0


async def test_a_redelivered_refund_event_is_applied_once(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    refund = _refund(customer, 1900)
    await _post(async_client, refund)
    assert (await _post(async_client, refund))["status"] == "duplicate"
    assert await _balance(test_db, intent) == 0


async def test_refund_after_spending_leaves_a_negative_balance(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """60 of 100 credits were spent: the full refund reverses all 100, the
    balance is -60 (owed), and further spending is refused."""
    from sandloom_billing.exceptions import InsufficientCreditsError
    from sandloom_billing.models import BillingCustomer

    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    owner = await test_db.get(BillingCustomer, intent.customer_id)
    assert owner is not None
    await BillingService.spend_credits(owner, 60, "use-60", test_db)
    await test_db.commit()

    await _post(async_client, _refund(customer, 1900))

    assert await _balance(test_db, intent) == -60
    with pytest.raises(InsufficientCreditsError):
        await BillingService.spend_credits(owner, 1, "use-1", test_db)


async def test_refund_from_another_psp_customer_reverses_nothing(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Ownership like ``invoice.paid``: the charge's PSP customer must own the
    grant the payment intent names."""
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)

    result = await _post(async_client, _refund("cus_someone_else", 1900))

    assert result["status"] == "failed", result
    assert await _balance(test_db, intent) == 100


async def test_refund_of_an_unknown_payment_is_skipped(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """E.g. a subscription invoice's charge: no credit grant to reverse
    (subscription access follows Stripe's subscription events)."""
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    other = _refund(customer, 1900)
    other["data"]["object"]["payment_intent"] = "pi_subscription_invoice"

    # Recorded (a grant may still arrive), nothing reversed.
    assert (await _post(async_client, other))["status"] == "processed"
    assert await _balance(test_db, intent) == 100


async def test_dispute_reverses_the_credits_and_a_won_dispute_restores_them(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)

    opened = await _post(
        async_client, _dispute("charge.dispute.created", "needs_response")
    )
    assert opened["status"] == "processed", opened
    assert await _balance(test_db, intent) == 0

    won = await _post(async_client, _dispute("charge.dispute.closed", "won"))
    assert won["status"] == "processed", won
    assert await _balance(test_db, intent) == 100
    again = await _post(async_client, _dispute("charge.dispute.closed", "won"))
    assert again["status"] == "ignored"
    assert await _balance(test_db, intent) == 100


async def test_a_lost_dispute_keeps_the_credits_reversed(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)
    await _post(async_client, _dispute("charge.dispute.created", "needs_response"))

    lost = await _post(async_client, _dispute("charge.dispute.closed", "lost"))

    assert lost["status"] in ("processed", "ignored"), lost
    assert await _balance(test_db, intent) == 0


async def test_refund_after_a_dispute_never_reverses_more_than_granted(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    await _post(async_client, _dispute("charge.dispute.created", "needs_response"))

    await _post(async_client, _refund(customer, 1900))

    assert await _balance(test_db, intent) == 0


async def test_dispute_of_an_unknown_payment_is_audited_for_an_operator(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A dispute on a charge billing granted nothing for (e.g. a subscription
    invoice): no automatic access change — audited for an operator."""
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)
    other = _dispute("charge.dispute.created", "needs_response", "dp_other")
    other["data"]["object"]["payment_intent"] = "pi_subscription_invoice"

    # Recorded (a grant may still arrive), audited, nothing reversed.
    assert (await _post(async_client, other))["status"] == "processed"
    assert await _balance(test_db, intent) == 100
    assert await _audits(test_db, "billing.charge.disputed") == 1


async def _channel_count(db: AsyncSession, channel: str) -> int:
    count = await db.execute(
        text("SELECT count(*) FROM outbox_messages WHERE channel = :c"),
        {"c": channel},
    )
    return int(count.scalar_one())


async def test_won_dispute_restores_with_a_distinct_event(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _bought_pack(async_client, catalog, fake_psp, sessions)
    await _post(async_client, _dispute("charge.dispute.created", "needs_response"))
    await _post(async_client, _dispute("charge.dispute.closed", "won"))

    assert await _channel_count(test_db, "billing.credits.reversed.v1") == 1
    assert await _channel_count(test_db, "billing.credits.restored.v1") == 1
    assert await _channel_count(test_db, "billing.credits.added.v1") == 1  # grant


# --------------------------------------------------------------------------- #
# Out of order: the refund / dispute arrives before the grant
# --------------------------------------------------------------------------- #


async def test_refund_before_the_grant_is_applied_when_the_grant_arrives(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The refund's state is persisted (journaled processed, nothing to
    reverse yet); the completion then grants and reverses in ONE
    transaction — the redelivery of the refund is no longer needed."""
    intent, customer = await _bought_pack(
        async_client,
        catalog,
        fake_psp,
        sessions,
        before_completion=[lambda cus: _refund(cus, 1900)],
    )

    assert await _balance(test_db, intent) == 0
    rows = (
        (
            await test_db.execute(
                select(CreditLedgerEntry.delta).order_by(CreditLedgerEntry.id)
            )
        )
        .scalars()
        .all()
    )
    assert list(rows) == [100, -100]
    # Redeliveries stay idempotent.
    assert (await _post(async_client, _refund(customer, 1900)))["status"] == "ignored"
    assert await _balance(test_db, intent) == 0


async def test_dispute_before_the_grant_is_applied_when_the_grant_arrives(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, _ = await _bought_pack(
        async_client,
        catalog,
        fake_psp,
        sessions,
        before_completion=[_dispute("charge.dispute.created", "needs_response")],
    )
    assert await _balance(test_db, intent) == 0

    won = await _post(async_client, _dispute("charge.dispute.closed", "won"))
    assert won["status"] == "processed"
    assert await _balance(test_db, intent) == 100


async def test_early_refund_from_another_psp_customer_is_never_applied(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Ownership holds out of order too: the recorded refund names another
    PSP customer than the grant's, so the grant's credits stay."""
    intent, _ = await _bought_pack(
        async_client,
        catalog,
        fake_psp,
        sessions,
        before_completion=[lambda _cus: _refund("cus_someone_else", 1900)],
    )

    assert await _balance(test_db, intent) == 100
    assert await _audits(test_db, "billing.credits.reversal_refused") == 1


# --------------------------------------------------------------------------- #
# Dispute policy: inquiries, partial disputes, refund + dispute
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["warning_needs_response", "warning_under_review"])
async def test_an_inquiry_reverses_nothing_and_is_audited(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    """An inquiry (``warning_*``) withdraws no funds: audit only."""
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)

    result = await _post(async_client, _dispute("charge.dispute.created", status))

    assert result["status"] == "ignored", result
    assert await _balance(test_db, intent) == 100
    assert await _audits(test_db, "billing.charge.disputed") == 1


async def test_an_inquiry_escalated_to_a_dispute_reverses(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)
    await _post(
        async_client, _dispute("charge.dispute.created", "warning_needs_response")
    )

    await _post(async_client, _dispute("charge.dispute.updated", "needs_response"))

    assert await _balance(test_db, intent) == 0


async def test_a_partial_dispute_reverses_a_proportional_share(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Disputing 5.00 of 19.00: ceil(100 * 500 / 1900) = 27 credits."""
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)

    await _post(
        async_client,
        _dispute("charge.dispute.created", "needs_response", amount=500),
    )

    assert await _balance(test_db, intent) == 100 - 27


async def test_refund_plus_dispute_never_reverses_more_than_granted(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Half refunded, the other half disputed, then the dispute lost and a
    stray full-amount dispute update: at most the 100 granted credits."""
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    await _post(async_client, _refund(customer, 950))
    assert await _balance(test_db, intent) == 50
    await _post(
        async_client,
        _dispute("charge.dispute.created", "needs_response", amount=950),
    )
    assert await _balance(test_db, intent) == 0
    await _post(async_client, _dispute("charge.dispute.closed", "lost", amount=1900))

    assert await _balance(test_db, intent) == 0


async def test_refund_without_a_psp_customer_is_refused(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A refunded charge must name its PSP customer (ownership check)."""
    intent, _ = await _bought_pack(async_client, catalog, fake_psp, sessions)
    refund = _refund("unused", 1900)
    refund["data"]["object"]["customer"] = None

    assert (await _post(async_client, refund))["status"] == "failed"
    assert await _balance(test_db, intent) == 100


# --------------------------------------------------------------------------- #
# Race: the payment-state row is created concurrently by both paths
# --------------------------------------------------------------------------- #


async def test_concurrent_first_refund_and_completion_both_converge(
    test_engine: Any, catalog: dict[str, Any]
) -> None:
    """Two REAL transactions, READ COMMITTED, no state row yet: the refund
    records the state (and sees no grant — the completion's is uncommitted)
    while the completion creates the grant. The completion must wait for the
    refund's state row and apply it; otherwise both commit and the refund is
    never applied."""
    import asyncio

    from sandloom.sdk.testing import committing_uow
    from sandloom_billing.services.webhook_service import (
        CHECKOUT_COMPLETED,
        WebhookService,
    )

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    external = f"cus_race_{uuid.uuid4().hex[:8]}"
    async with committing_uow(maker) as s:
        customer = await BillingService.get_or_create_customer(s, user_id=uuid.uuid4())
        customer.stripe_customer_id = external
        customer_id = customer.id
    pack = catalog["pack_price"]
    session_obj = {
        "id": f"cs_race_{uuid.uuid4().hex[:8]}",
        "mode": "payment",
        "payment_status": "paid",
        "payment_intent": PI,
        "amount_total": pack.amount,
        "currency": "usd",
        "customer": external,
        "metadata": {"customer_id": str(customer_id), "price_id": str(pack.id)},
    }
    refund = _refund(external, 1900)

    async def completion() -> None:
        async with committing_uow(maker) as b:
            await WebhookService._apply_checkout(
                CHECKOUT_COMPLETED, session_obj, b, event_id="evt_race_completion"
            )

    async with committing_uow(maker) as a:
        await WebhookService._apply_charge_refunded(refund, refund["data"]["object"], a)
        racing = asyncio.create_task(completion())
        await asyncio.sleep(0.5)  # the completion runs while the refund is open
    await asyncio.wait_for(racing, timeout=10)

    async with maker() as s:
        assert await BillingService.get_credit_balance(customer_id, s) == 0


async def test_housekeeping_purges_only_old_orphan_payment_states(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Payment-state rows that never matched a grant (subscription-invoice
    refunds / disputes) are purged after the retention; rows of a grant and
    recent orphans stay."""
    from sandloom_billing import tasks
    from sandloom_billing.services.webhook_service import purge_orphan_payment_states

    _, customer = await _bought_pack(
        async_client,
        catalog,
        fake_psp,
        sessions,
        before_completion=[lambda cus: _refund(cus, 950)],
    )
    for pi in ("pi_old_orphan", "pi_new_orphan"):
        orphan = _refund(customer, 100)
        orphan["data"]["object"]["payment_intent"] = pi
        await _post(async_client, orphan)
    await test_db.execute(
        text(
            "UPDATE billing_psp_payment_states SET updated_at = now() - "
            "interval '400 days' WHERE psp_payment_intent_id IN "
            "('pi_old_orphan', :granted)"
        ),
        {"granted": PI},
    )

    assert await purge_orphan_payment_states(test_db) == 1
    left = (
        await test_db.execute(
            text("SELECT psp_payment_intent_id FROM billing_psp_payment_states")
        )
    ).scalars()
    assert sorted(left) == sorted([PI, "pi_new_orphan"])
    # The periodic task runs it (and reports the count).
    assert "purged_payment_states" in await tasks.reconcile_intents_once(fake_psp)


async def test_refund_in_another_currency_is_refused(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    refund = _refund(customer, 1900)
    refund["data"]["object"]["currency"] = "eur"

    assert (await _post(async_client, refund))["status"] == "failed"
    assert await _balance(test_db, intent) == 100


@pytest.mark.parametrize("kind", ["refund", "dispute"])
async def test_refund_or_dispute_without_a_currency_is_refused(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
    kind: str,
) -> None:
    """A missing currency cannot be checked: refused like a missing customer."""
    intent, customer = await _bought_pack(async_client, catalog, fake_psp, sessions)
    event = (
        _refund(customer, 1900)
        if kind == "refund"
        else _dispute("charge.dispute.created", "needs_response")
    )
    del event["data"]["object"]["currency"]

    assert (await _post(async_client, event))["status"] == "failed"
    assert await _balance(test_db, intent) == 100


async def test_purge_never_deletes_the_state_a_concurrent_completion_holds(
    test_engine: Any, catalog: dict[str, Any]
) -> None:
    """An orphan state row older than the retention, and a completion for its
    payment running right now (row locked, grant not committed yet): the
    purge waits for the lock and must then keep the row — the completion has
    bound it to its grant."""
    import asyncio
    from datetime import UTC, datetime

    from sandloom.sdk.testing import committing_uow
    from sandloom_billing.services.webhook_service import (
        CHECKOUT_COMPLETED,
        WebhookService,
        purge_orphan_payment_states,
    )

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    external = f"cus_purge_{uuid.uuid4().hex[:8]}"
    async with committing_uow(maker) as s:
        customer = await BillingService.get_or_create_customer(s, user_id=uuid.uuid4())
        customer.stripe_customer_id = external
        customer_id = customer.id
    refund = _refund(external, 1900)
    async with committing_uow(maker) as s:
        await WebhookService._apply_charge_refunded(refund, refund["data"]["object"], s)
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE billing_psp_payment_states SET updated_at = now() - "
                "interval '400 days' WHERE psp_payment_intent_id = :p"
            ),
            {"p": PI},
        )
        await s.commit()
    pack = catalog["pack_price"]
    holding = asyncio.Event()
    release = asyncio.Event()

    async def completion() -> None:
        async with committing_uow(maker) as b:
            await WebhookService._apply_checkout(
                CHECKOUT_COMPLETED,
                {
                    "id": f"cs_purge_{uuid.uuid4().hex[:8]}",
                    "mode": "payment",
                    "payment_status": "paid",
                    "payment_intent": PI,
                    "amount_total": pack.amount,
                    "currency": "usd",
                    "customer": external,
                    "metadata": {
                        "customer_id": str(customer_id),
                        "price_id": str(pack.id),
                    },
                },
                b,
                event_id="evt_purge_completion",
            )
            holding.set()
            await release.wait()  # the state row stays locked meanwhile

    async def purge() -> int:
        async with committing_uow(maker) as p:
            return await purge_orphan_payment_states(p, now=datetime.now(UTC))

    completing = asyncio.create_task(completion())
    await asyncio.wait_for(holding.wait(), 10)
    purging = asyncio.create_task(purge())
    await asyncio.sleep(0.5)
    assert not purging.done()  # blocked on the completion's row lock
    release.set()
    await asyncio.wait_for(completing, 10)
    assert await asyncio.wait_for(purging, 10) == 0

    async with maker() as s:
        assert await BillingService.get_credit_balance(customer_id, s) == 0
        kept = await s.execute(
            text(
                "SELECT count(*) FROM billing_psp_payment_states "
                "WHERE psp_payment_intent_id = :p"
            ),
            {"p": PI},
        )
        assert int(kept.scalar_one()) == 1


async def test_paid_credit_pack_without_a_payment_intent_grants_nothing(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Credits are reversible only through the payment intent: a paid
    completion without one would grant credits no refund / dispute could ever
    find. Nothing is granted, the event is journaled failed, audited, and the
    intent flagged for an operator."""
    from sandloom_billing.models import CreditGrant, OperationIntent

    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pack_price"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [session] = fake_psp.checkout_sessions
    result = await _post(
        async_client,
        _event(
            "checkout.session.completed",
            {
                "id": session["id"],
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": catalog["pack_price"].amount,
                "currency": "usd",
                "customer": session["customer"],
                "metadata": session["metadata"],
            },
        ),
    )

    assert result["status"] == "failed", result
    [intent] = await _intents(sessions)
    assert await _balance(test_db, intent) == 0
    grants = await test_db.execute(select(CreditGrant))
    assert grants.scalars().all() == []
    assert await _audits(test_db, "billing.credits.grant_refused") == 1
    flagged = await test_db.get(OperationIntent, intent.id, populate_existing=True)
    assert flagged is not None and flagged.followup_at is not None
