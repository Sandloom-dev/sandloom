"""Webhook hardening: forgery, ordering, invoice.paid, credit packs, 2xx failures."""

import json
import time
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import set_environment
from sandloom_billing.models import BillingSubscription, StripeEvent
from sandloom_billing.psp import (
    FakeStripeClient,
    StripeHttpClient,
    StripeSignatureError,
    psp_configuration_error,
    sign_stripe_payload,
)
from sandloom_billing.services import BillingService
from sandloom_billing.tests.conftest import WEBHOOK_SECRET

HOOK = "/api/v1/billing/webhooks/stripe"
NOW = int(time.time())


@pytest.fixture
def _intentless_subscriptions_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in for the tests below that assert ACCESS from an intent-less
    event (no ``sandloom_intent_id``): granting those is an explicit policy
    (ADR-0003 open item 9, default off). Every other test in this module runs
    under the default (intent-less subscriptions grant nothing)."""
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "billing_grant_intentless_subscriptions", True)


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)}


async def _post(client: AsyncClient, payload: dict) -> Any:
    body, headers = _signed(payload)
    return await client.post(HOOK, content=body, headers=headers)


def _sub_event(
    event_type: str,
    *,
    created: int | None,
    status: str = "active",
    sub_id: str = "sub_h1",
    customer: str = "cus_h",
    period_end: int | None = NOW + 86400 * 30,
    basil: bool = False,
) -> dict:
    obj: dict[str, Any] = {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "cancel_at_period_end": False,
        "items": {"data": [{"price": {"id": "price_pro_month"}}]},
    }
    if basil:
        obj["items"]["data"][0]["current_period_end"] = period_end
    else:
        obj["current_period_end"] = period_end
    event: dict[str, Any] = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": event_type,
        "data": {"object": obj},
    }
    if created is not None:
        event["created"] = created
    return event


def _invoice_event(
    *, created: int, sub_id: str = "sub_h1", basil: bool = False, line_end: int
) -> dict:
    obj: dict[str, Any] = {
        "id": f"in_{uuid.uuid4().hex[:8]}",
        "customer": "cus_h",
        "lines": {"data": [{"period": {"start": NOW, "end": line_end}}]},
        # Stripe quirk: the invoice's own period is the PREVIOUS period.
        "period_end": NOW - 86400,
    }
    if basil:
        obj["parent"] = {"subscription_details": {"subscription": sub_id}}
    else:
        obj["subscription"] = sub_id
    return {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "invoice.paid",
        "created": created,
        "data": {"object": obj},
    }


def _checkout_event(
    event_type: str,
    *,
    customer: Any,
    price_id: int,
    payment_status: str = "paid",
    session_id: str = "cs_pack_1",
    extra_metadata: dict[str, str] | None = None,
    amount_total: int | None = 1900,  # the catalog pack price (USD 19.00)
    currency: str | None = "usd",
) -> dict:
    return {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": event_type,
        "created": NOW,
        "data": {
            "object": {
                "id": session_id,
                "mode": "payment",
                "payment_status": payment_status,
                # One payment per session (Stripe): refunds find it by this.
                "payment_intent": f"pi_{session_id}",
                "amount_total": amount_total,
                "currency": currency,
                "customer": "cus_h",
                "metadata": {
                    "customer_id": str(customer.id),
                    "price_id": str(price_id),
                    **(extra_metadata or {}),
                },
            }
        },
    }


async def _customer(test_db: AsyncSession, external_id: str = "cus_h") -> Any:
    customer = await BillingService.get_or_create_customer(
        test_db, user_id=uuid.uuid4()
    )
    customer.stripe_customer_id = external_id
    await test_db.commit()
    return customer


async def _sub(test_db: AsyncSession, sub_id: str = "sub_h1") -> BillingSubscription:
    result = await test_db.execute(
        select(BillingSubscription)
        .where(BillingSubscription.stripe_subscription_id == sub_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _journal(test_db: AsyncSession, event_id: str) -> StripeEvent:
    result = await test_db.execute(
        select(StripeEvent)
        .where(StripeEvent.event_id == event_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# --------------------------------------------------------------------------- #
# Critical #1 — forged webhooks without a secret
# --------------------------------------------------------------------------- #


class TestForgedWebhooks:
    def test_fake_without_secret_rejects_webhooks(self) -> None:
        psp = FakeStripeClient(webhook_secret=None)
        with pytest.raises(StripeSignatureError):
            psp.verify_signature(b'{"id":"evt","type":"x"}', "")

    def test_stripe_client_without_secret_raises_signature_error(self) -> None:
        with pytest.raises(StripeSignatureError):
            StripeHttpClient("sk_test").verify_signature(b"{}", "")

    async def test_forged_event_rejected_over_http(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        from sandloom_billing.psp import install_psp_client

        customer = await _customer(test_db)
        install_psp_client(FakeStripeClient(None))  # restored by async_client
        forged = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
        )
        resp = await async_client.post(HOOK, content=json.dumps(forged).encode())
        assert resp.status_code == 400
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0

    def test_prod_refuses_fake_psp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sandloom.sdk.infra import settings

        monkeypatch.setattr(settings, "sandloom_accepted_risks", [])
        monkeypatch.setattr(settings, "billing_provider", "fake")
        set_environment(monkeypatch, "dev")
        assert psp_configuration_error(settings) is None

        set_environment(monkeypatch, "prod")
        error = psp_configuration_error(settings)
        assert error is not None and "BILLING_PROVIDER=fake" in error

        monkeypatch.setattr(settings, "sandloom_accepted_risks", ["fake_psp_in_prod"])
        assert psp_configuration_error(settings) is None  # explicit demo opt-in
        monkeypatch.setattr(settings, "sandloom_accepted_risks", [])

        monkeypatch.setattr(settings, "billing_provider", "stripe")
        assert psp_configuration_error(settings) is None

    async def test_register_adds_critical_startup_guard_in_prod(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sandloom.sdk.infra import settings
        from sandloom_billing import register

        monkeypatch.setattr(settings, "billing_provider", "fake")
        set_environment(monkeypatch, "prod")
        monkeypatch.setattr(settings, "sandloom_accepted_risks", [])

        from unittest.mock import MagicMock

        recorder = MagicMock()
        register(recorder)
        calls = recorder.add_startup_task.call_args_list
        assert len(calls) == 1
        task = calls[0].args[0]
        assert calls[0].kwargs == {"critical": True}
        with pytest.raises(RuntimeError, match="BILLING_PROVIDER=fake"):
            await task()


# --------------------------------------------------------------------------- #
# High #2 — out-of-order / replayed subscription events
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestSubscriptionOrdering:
    async def test_stale_update_does_not_roll_back(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await _post(
            async_client,
            _sub_event("customer.subscription.created", created=NOW - 100),
        )
        newer = _sub_event(
            "customer.subscription.updated", created=NOW, status="past_due"
        )
        assert (await _post(async_client, newer)).json()["status"] == "processed"
        older = _sub_event(
            "customer.subscription.updated", created=NOW - 50, status="active"
        )
        resp = await _post(async_client, older)
        assert resp.status_code == 200 and resp.json()["status"] == "ignored"
        sub = await _sub(test_db)
        assert sub.status == "past_due"
        assert sub.last_event_at == NOW and sub.last_event_id == newer["id"]
        assert "stale" in (await _journal(test_db, older["id"])).note

    async def test_deleted_then_newer_updated_stays_canceled(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        await _post(
            async_client,
            _sub_event("customer.subscription.created", created=NOW - 100),
        )
        await _post(
            async_client,
            _sub_event(
                "customer.subscription.deleted", created=NOW - 10, status="canceled"
            ),
        )
        resurrect = _sub_event(
            "customer.subscription.updated", created=NOW, status="active"
        )
        assert (await _post(async_client, resurrect)).json()["status"] == "ignored"
        assert (await _sub(test_db)).status == "canceled"
        assert not await BillingService.check_subscription_active(customer.id, test_db)

    async def test_deleted_applies_even_if_delivered_late(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await _post(
            async_client, _sub_event("customer.subscription.created", created=NOW)
        )
        late_delete = _sub_event(
            "customer.subscription.deleted", created=NOW - 5, status="canceled"
        )
        assert (await _post(async_client, late_delete)).json()["status"] == "processed"
        assert (await _sub(test_db)).status == "canceled"

    async def test_created_after_updated_is_ignored(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await _post(
            async_client,
            _sub_event("customer.subscription.updated", created=NOW, status="active"),
        )
        late_created = _sub_event(
            "customer.subscription.created", created=NOW, status="incomplete"
        )
        assert (await _post(async_client, late_created)).json()["status"] == "ignored"
        assert (await _sub(test_db)).status == "active"

    async def test_event_without_created_cannot_override_history(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await _post(
            async_client, _sub_event("customer.subscription.created", created=NOW)
        )
        undated = _sub_event(
            "customer.subscription.updated", created=None, status="past_due"
        )
        assert (await _post(async_client, undated)).json()["status"] == "ignored"
        assert (await _sub(test_db)).status == "active"


# --------------------------------------------------------------------------- #
# Medium #10 — basil API shape / missing period end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestStripeShapes:
    @pytest.mark.usefixtures("_intentless_subscriptions_grant")
    async def test_basil_item_period_end_grants_access(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        end = NOW + 86400 * 10
        await _post(
            async_client,
            _sub_event(
                "customer.subscription.created", created=NOW, basil=True, period_end=end
            ),
        )
        sub = await _sub(test_db)
        assert sub.current_period_end is not None
        assert int(sub.current_period_end.timestamp()) == end
        assert await BillingService.check_subscription_active(customer.id, test_db)

    async def test_missing_period_end_is_not_perpetual_access(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        await _post(
            async_client,
            _sub_event("customer.subscription.created", created=NOW, period_end=None),
        )
        assert (await _sub(test_db)).status == "active"
        assert not await BillingService.check_subscription_active(customer.id, test_db)


# --------------------------------------------------------------------------- #
# High #3 — invoice.paid
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestInvoicePaid:
    async def _with_status(
        self, client: AsyncClient, status: str, created: int = NOW - 100
    ) -> None:
        await _post(
            client,
            _sub_event(
                "customer.subscription.created",
                created=created,
                status=status,
                period_end=NOW + 3600,
            ),
        )

    @pytest.mark.usefixtures("_intentless_subscriptions_grant")
    async def test_past_due_recovers_and_period_advances(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        await self._with_status(async_client, "past_due")
        new_end = NOW + 86400 * 30
        resp = await _post(async_client, _invoice_event(created=NOW, line_end=new_end))
        assert resp.json()["status"] == "processed"
        sub = await _sub(test_db)
        assert sub.status == "active"
        assert int(sub.current_period_end.timestamp()) == new_end
        assert await BillingService.check_subscription_active(customer.id, test_db)

    async def test_basil_invoice_parent_shape(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await self._with_status(async_client, "unpaid")
        await _post(
            async_client,
            _invoice_event(created=NOW, basil=True, line_end=NOW + 86400),
        )
        assert (await _sub(test_db)).status == "active"

    async def test_canceled_is_never_resurrected(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        await self._with_status(async_client, "active")
        await _post(
            async_client,
            _sub_event(
                "customer.subscription.deleted", created=NOW - 50, status="canceled"
            ),
        )
        resp = await _post(
            async_client, _invoice_event(created=NOW, line_end=NOW + 86400 * 30)
        )
        assert resp.json()["status"] == "ignored"
        assert (await _sub(test_db)).status == "canceled"
        assert not await BillingService.check_subscription_active(customer.id, test_db)

    async def test_trialing_zero_invoice_does_not_flip_status(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await self._with_status(async_client, "trialing")
        await _post(async_client, _invoice_event(created=NOW, line_end=NOW + 3600))
        assert (await _sub(test_db)).status == "trialing"

    async def test_stale_invoice_ignored(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        await _customer(test_db)
        await self._with_status(async_client, "past_due", created=NOW)
        resp = await _post(
            async_client, _invoice_event(created=NOW - 60, line_end=NOW + 86400)
        )
        assert resp.json()["status"] == "ignored"
        assert (await _sub(test_db)).status == "past_due"


# --------------------------------------------------------------------------- #
# High #4 — credit packs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestCreditPackCheckout:
    async def test_grant_comes_from_catalog_not_metadata(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
            extra_metadata={"credits": "999999"},
        )
        assert (await _post(async_client, event)).json()["status"] == "processed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 100

    async def test_real_checkout_metadata_round_trip(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog, fake_psp
    ) -> None:
        """The metadata create_checkout_session writes is enough to grant."""
        customer = await _customer(test_db)
        await BillingService.create_checkout_session(
            customer, catalog["pack_price"].id, fake_psp, test_db
        )
        metadata = fake_psp.checkout_sessions[0]["metadata"]
        assert "credits" not in metadata
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=int(metadata["price_id"]),
        )
        await _post(async_client, event)
        assert await BillingService.get_credit_balance(customer.id, test_db) == 100

    async def test_unpaid_completion_grants_nothing_until_async_success(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        price_id = catalog["pack_price"].id
        pending = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=price_id,
            payment_status="unpaid",
        )
        assert (await _post(async_client, pending)).json()["status"] == "ignored"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0

        succeeded = _checkout_event(
            "checkout.session.async_payment_succeeded",
            customer=customer,
            price_id=price_id,
        )
        assert (await _post(async_client, succeeded)).json()["status"] == "processed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 100

        # A paid completion for the same session is an idempotent replay.
        again = _checkout_event(
            "checkout.session.completed", customer=customer, price_id=price_id
        )
        await _post(async_client, again)
        assert await BillingService.get_credit_balance(customer.id, test_db) == 100

    async def test_async_payment_failed_is_recorded_without_grant(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        failed = _checkout_event(
            "checkout.session.async_payment_failed",
            customer=customer,
            price_id=catalog["pack_price"].id,
            payment_status="unpaid",
        )
        assert (await _post(async_client, failed)).json()["status"] == "processed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0
        assert "failed" in (await _journal(test_db, failed["id"])).note

    async def test_subscription_price_grants_no_credits(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pro_month"].id,
            extra_metadata={"credits": "500"},
        )
        assert (await _post(async_client, event)).json()["status"] == "ignored"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0


# --------------------------------------------------------------------------- #
# Medium #13 / Low — permanent data errors answer 2xx and are journaled
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestPermanentFailuresAck:
    async def test_unknown_customer_is_recorded_failed_with_2xx(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        event = _sub_event(
            "customer.subscription.created", created=NOW, customer="cus_nobody"
        )
        resp = await _post(async_client, event)
        assert resp.status_code == 200 and resp.json()["status"] == "failed"
        row = await _journal(test_db, event["id"])
        assert row.status == "failed" and "cus_nobody" in row.note

    async def test_bad_price_metadata_is_recorded_failed_with_2xx(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed", customer=customer, price_id=0
        )
        event["data"]["object"]["metadata"]["price_id"] = "-not-a-number"
        resp = await _post(async_client, event)
        assert resp.status_code == 200 and resp.json()["status"] == "failed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0

    async def test_customer_mismatch_is_rejected(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
        )
        event["data"]["object"]["metadata"]["customer_id"] = str(uuid.uuid4())
        assert (await _post(async_client, event)).json()["status"] == "failed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0


# --------------------------------------------------------------------------- #
# Test gap — the REAL request UoW (no get_db override)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestWebhookThroughRealUoW:
    @pytest.mark.sandloom_allow_server_errors  # asserts the masked 500 itself
    async def test_transient_failure_rolls_back_everything(
        self,
        uow_client: AsyncClient,
        test_db: AsyncSession,
        catalog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        customer = await _customer(test_db)
        customer_id = customer.id
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
        )
        real_add = BillingService.add_credits.__func__  # type: ignore[attr-defined]

        async def _add_then_crash(cls: Any, *args: Any, **kwargs: Any) -> int:
            await real_add(cls, *args, **kwargs)
            raise RuntimeError("database went away")

        monkeypatch.setattr(BillingService, "add_credits", classmethod(_add_then_crash))
        resp = await _post(uow_client, event)
        assert resp.status_code == 500

        # The UoW rolled back the grant AND the journal row (Stripe retries).
        test_db.expire_all()
        assert await BillingService.get_credit_balance(customer_id, test_db) == 0
        journal = await test_db.execute(
            select(StripeEvent).where(StripeEvent.event_id == event["id"])
        )
        assert journal.scalar_one_or_none() is None

        monkeypatch.undo()
        retry = await _post(uow_client, event)
        assert retry.status_code == 200 and retry.json()["status"] == "processed"
        test_db.expire_all()
        assert await BillingService.get_credit_balance(customer_id, test_db) == 100

    async def test_permanent_failure_commits_failed_journal(
        self, uow_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        event = _sub_event(
            "customer.subscription.created", created=NOW, customer="cus_ghost"
        )
        resp = await _post(uow_client, event)
        assert resp.status_code == 200 and resp.json()["status"] == "failed"
        row = await _journal(test_db, event["id"])
        assert row.status == "failed"
        # A redelivery is a duplicate, not another failure.
        assert (await _post(uow_client, event)).json()["status"] == "duplicate"


# --------------------------------------------------------------------------- #
# Review — checkout amount/currency/customer binding, same-second ties
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestCheckoutAmountAndCustomerBinding:
    @pytest.mark.parametrize(
        ("amount_total", "currency"),
        [(1, "usd"), (1900, "eur"), (None, "usd"), (1900, None)],
    )
    async def test_amount_or_currency_mismatch_grants_nothing(
        self,
        async_client: AsyncClient,
        test_db: AsyncSession,
        catalog,
        amount_total: int | None,
        currency: str | None,
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
            amount_total=amount_total,
            currency=currency,
        )
        resp = await _post(async_client, event)
        assert resp.status_code == 200 and resp.json()["status"] == "failed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0

    async def test_currency_is_compared_case_insensitively(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
            currency="USD",
        )
        assert (await _post(async_client, event)).json()["status"] == "processed"
        assert await BillingService.get_credit_balance(customer.id, test_db) == 100

    async def test_metadata_customer_id_is_required(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog
    ) -> None:
        customer = await _customer(test_db)
        event = _checkout_event(
            "checkout.session.completed",
            customer=customer,
            price_id=catalog["pack_price"].id,
        )
        del event["data"]["object"]["metadata"]["customer_id"]
        resp = await _post(async_client, event)
        assert resp.json()["status"] == "failed"
        assert "customer_id" in (await _journal(test_db, event["id"])).note
        assert await BillingService.get_credit_balance(customer.id, test_db) == 0


@pytest.mark.asyncio
class TestSameSecondTie:
    async def test_tie_applies_the_psps_current_state(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog, fake_psp
    ) -> None:
        """Two events in one second: the re-fetched PSP state wins, not order."""
        await _customer(test_db)
        await _post(
            async_client,
            _sub_event("customer.subscription.created", created=NOW, status="active"),
        )
        # The PSP's truth after both same-second changes: past_due.
        fake_psp.subscriptions["sub_h1"] = _sub_event(
            "customer.subscription.updated", created=NOW, status="past_due"
        )["data"]["object"]
        # A delayed same-second event still carrying the OLD state.
        late = _sub_event("customer.subscription.updated", created=NOW, status="active")
        assert (await _post(async_client, late)).json()["status"] == "processed"
        assert (await _sub(test_db)).status == "past_due"

    async def test_tie_without_retrieve_falls_back_to_delivery_order(
        self, async_client: AsyncClient, test_db: AsyncSession, catalog, fake_psp
    ) -> None:
        await _customer(test_db)
        await _post(
            async_client,
            _sub_event("customer.subscription.created", created=NOW, status="active"),
        )
        # fake_psp.subscriptions is empty: retrieve raises -> delivery order.
        tie = _sub_event(
            "customer.subscription.updated", created=NOW, status="past_due"
        )
        assert (await _post(async_client, tie)).json()["status"] == "processed"
        assert (await _sub(test_db)).status == "past_due"
