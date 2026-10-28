"""Stripe webhook processing (BILLING_SPEC §9).

Pipeline: verify signature → parse → idempotency journal insert → apply →
publish billing.* events → mark processed/skipped/failed.

Guarantees:

- **Monotonic subscriptions.** Each subscription row stores the ``created``
  timestamp + id of the last applied PSP event; an older (out-of-order or
  replayed) event never rolls the state back. ``canceled`` and
  ``incomplete_expired`` are terminal — a
  canceled PSP subscription never comes back (a resubscription gets a new id).
  Ordering limitation: Stripe's ``created`` has 1-second resolution, so two
  events of the same second cannot be ordered by timestamp (the guard is a
  strict ``<``). On such a tie the subscription is RE-FETCHED from the PSP
  (when the client implements the optional ``retrieve_subscription``) and its
  current state is applied instead of the event payload; a client without it
  (or a failed fetch) falls back to delivery order. The fetch runs while the
  subscription row lock is held — ties are rare, and correctness wins.
  ``customer.subscription.created`` never overwrites an existing row.
- **Permanent data problems answer 2xx.** An event that can never apply
  (unknown customer, bad metadata, unknown price) is journaled as ``failed``
  with the reason and acknowledged — answering 4xx would make Stripe retry for
  days and eventually disable the endpoint. Transient errors (DB) still raise,
  so the UoW rolls back and Stripe redelivers.
- **Credits come from the catalog.** A credit-pack purchase grants the
  ``credits`` of the purchased price (resolved by our price id), only once the
  payment is actually ``paid`` — never an amount taken from metadata. The
  session must also carry ``metadata.customer_id`` matching the session's PSP
  customer, and its ``amount_total`` + ``currency`` must equal the catalog
  price (credit-pack checkouts are created without discounts or tax, so any
  difference means the session was not ours to honour): otherwise the event is
  journaled ``failed`` for operator review and nothing is granted.
- **Only authorized intents complete** (ADR-0003 G1). A session / subscription
  created by billing carries ``metadata.sandloom_intent_id``; a credit grant or a
  NEW subscription row is applied only when that operation intent is
  ``issued`` (its URL was handed out after the authority re-check) and belongs
  to the event's customer. A completion for a revoked, pending, failed or
  unknown intent is NOT applied: the event is journaled ``failed``, an audit
  record ``billing.intent.unauthorized_completion`` is written (required) and
  the intent is flagged (``followup_at``) for an operator — the money may have
  moved; refunding it is a manual operator step, out of scope here. Objects
  WITHOUT the marker (sessions created before intents existed, or by an
  operator in the PSP dashboard) apply exactly as before.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom.sdk.infra import settings
from sandloom_billing import events
from sandloom_billing.models import (
    DISPUTE_INQUIRY,
    DISPUTE_LOST,
    DISPUTE_OPEN,
    DISPUTE_TERMINAL,
    DISPUTE_WON,
    EVENT_STATUS_FAILED,
    EVENT_STATUS_PROCESSED,
    EVENT_STATUS_SKIPPED,
    INTENT_ISSUED,
    INTENT_METADATA_KEY,
    OPERATION_CHECKOUT,
    OPERATION_CUSTOMER_PORTAL,
    ORIGIN_INTENT,
    ORIGIN_PSP,
    PRODUCT_TYPE_ONE_TIME,
    PSP_SUBSCRIPTION_STATUSES,
    RECOVERABLE_STATUSES,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PAST_DUE,
    STATUS_TRIALING,
    BillingCustomer,
    BillingPrice,
    BillingProduct,
    BillingSubscription,
    CreditGrant,
    OperationIntent,
    PspPaymentState,
    StripeEvent,
    reversal_due,
)
from sandloom_billing.psp import StripeClientPort
from sandloom_billing.services.billing_service import BillingService

logger = logging.getLogger("sandloom_billing.webhooks")

SUBSCRIPTION_CREATED = "customer.subscription.created"
SUBSCRIPTION_UPDATED = "customer.subscription.updated"
SUBSCRIPTION_DELETED = "customer.subscription.deleted"
_SUBSCRIPTION_EVENTS = {
    SUBSCRIPTION_CREATED,
    SUBSCRIPTION_UPDATED,
    SUBSCRIPTION_DELETED,
}

CHECKOUT_COMPLETED = "checkout.session.completed"
CHECKOUT_ASYNC_SUCCEEDED = "checkout.session.async_payment_succeeded"
CHECKOUT_ASYNC_FAILED = "checkout.session.async_payment_failed"
_CHECKOUT_EVENTS = {CHECKOUT_COMPLETED, CHECKOUT_ASYNC_SUCCEEDED, CHECKOUT_ASYNC_FAILED}

INVOICE_PAID = "invoice.paid"

CHARGE_REFUNDED = "charge.refunded"
DISPUTE_CREATED = "charge.dispute.created"
DISPUTE_UPDATED = "charge.dispute.updated"
DISPUTE_CLOSED = "charge.dispute.closed"
_DISPUTE_EVENTS = {DISPUTE_CREATED, DISPUTE_UPDATED, DISPUTE_CLOSED}

#: Stripe dispute status -> the payment's dispute state. ``warning_*`` are
#: inquiries (no funds withdrawn, nothing reversed); an inquiry that escalates
#: arrives as ``charge.dispute.updated`` with ``needs_response``.
_DISPUTE_STATUS = {
    "warning_needs_response": DISPUTE_INQUIRY,
    "warning_under_review": DISPUTE_INQUIRY,
    "warning_closed": DISPUTE_INQUIRY,
    "needs_response": DISPUTE_OPEN,
    "under_review": DISPUTE_OPEN,
    "won": DISPUTE_WON,
    "lost": DISPUTE_LOST,
}

#: Audit actions of the credit reversal policy (ADR-0003 open item 12).
AUDIT_CREDITS_REVERSED = "billing.credits.reversed"
AUDIT_CREDITS_RESTORED = "billing.credits.restored"
AUDIT_CHARGE_DISPUTED = "billing.charge.disputed"
AUDIT_REVERSAL_REFUSED = "billing.credits.reversal_refused"
#: A paid credit-pack completion that could not be granted (no payment
#: intent to reverse it by).
AUDIT_GRANT_REFUSED = "billing.credits.grant_refused"

#: Audit action: a user-actionable subscription change no portal intent of the
#: customer accounts for (ADR-0003 open item 10).
AUDIT_UNATTRIBUTED_CHANGE = "billing.subscription.unattributed_change"

#: Clock skew allowed between billing's issue commit and the PSP's event time.
_ATTRIBUTION_SKEW = timedelta(minutes=5)

#: Audit action: the PSP completed an operation billing did not authorize.
AUDIT_UNAUTHORIZED_COMPLETION = "billing.intent.unauthorized_completion"

#: Audit action: a subscription maps to no catalog price (no entitlements).
AUDIT_UNKNOWN_PRICE = "billing.subscription.unknown_price"

_PAYING_STATUSES = frozenset({STATUS_ACTIVE, STATUS_TRIALING, STATUS_PAST_DUE})

_RESPONSE_STATUS = {
    EVENT_STATUS_PROCESSED: "processed",
    EVENT_STATUS_SKIPPED: "ignored",
    EVENT_STATUS_FAILED: "failed",
}


def _grants(sub: BillingSubscription) -> bool:
    """The access predicate (same as the live check and the claims)."""
    return sub.grants_access(
        deny_on_past_due=settings.billing_deny_on_past_due,
        allow_intentless=settings.billing_grant_intentless_subscriptions,
    )


async def _plan(sub: BillingSubscription, db: AsyncSession) -> dict[str, Any] | None:
    """The subscription's catalog plan for its events (``None``: no known
    catalog price), so a consumer can project the plan without a call."""
    if sub.price_id is None:
        return None
    row = (
        await db.execute(
            select(BillingPrice, BillingProduct)
            .join(BillingProduct, BillingProduct.id == BillingPrice.product_id)
            .where(BillingPrice.id == sub.price_id)
        )
    ).first()
    if row is None:
        return None
    price, product = row
    return {
        "product": product.name,
        "price_id": price.stripe_price_id,
        "interval": price.recurring_interval,
    }


class WebhookDataError(Exception):
    """The event can never be applied (permanent): journal as failed, ack 2xx."""


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: str
    note: str | None = None


def _processed(note: str | None = None) -> _Outcome:
    return _Outcome(EVENT_STATUS_PROCESSED, note)


def _skipped(note: str) -> _Outcome:
    return _Outcome(EVENT_STATUS_SKIPPED, note)


def _ref_id(value: object) -> str | None:
    """A Stripe reference: a plain id, or an expanded object carrying ``id``."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        inner = value.get("id")
        return inner if isinstance(inner, str) and inner else None
    return None


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _event_created(event: dict[str, Any]) -> int | None:
    created = event.get("created")
    if isinstance(created, bool) or not isinstance(created, int):
        return None
    return created


def subscription_period_end(sub_obj: dict[str, Any]) -> int | None:
    """``current_period_end`` from either Stripe API shape.

    Pre-basil: on the subscription. Basil (2025-03-31+): on each subscription
    item — the subscription's end is the latest item end.
    """
    top = sub_obj.get("current_period_end")
    if isinstance(top, int) and not isinstance(top, bool) and top > 0:
        return top
    ends = [
        item.get("current_period_end")
        for item in _as_list(_as_dict(sub_obj.get("items")).get("data"))
        if isinstance(item, dict)
    ]
    valid = [
        e for e in ends if isinstance(e, int) and not isinstance(e, bool) and e > 0
    ]
    return max(valid) if valid else None


def invoice_subscription_id(invoice_obj: dict[str, Any]) -> str | None:
    """The invoice's subscription id from either Stripe API shape.

    Pre-basil: ``invoice.subscription``. Basil: ``invoice.parent.
    subscription_details.subscription``.
    """
    legacy = _ref_id(invoice_obj.get("subscription"))
    if legacy:
        return legacy
    details = _as_dict(_as_dict(invoice_obj.get("parent")).get("subscription_details"))
    return _ref_id(details.get("subscription"))


def invoice_period_end(invoice_obj: dict[str, Any]) -> int | None:
    """End of the service period the invoice paid for (from its line items).

    ``invoice.period_end`` is NOT used: for subscription-cycle invoices it is
    the end of the *previous* period.
    """
    ends: list[int] = []
    for line in _as_list(_as_dict(invoice_obj.get("lines")).get("data")):
        end = _as_dict(_as_dict(line).get("period")).get("end")
        if isinstance(end, int) and not isinstance(end, bool) and end > 0:
            ends.append(end)
    return max(ends) if ends else None


class WebhookService:
    """Idempotent, monotonic webhook application."""

    @classmethod
    async def handle_stripe_webhook(
        cls,
        payload: bytes,
        signature_header: str,
        psp: StripeClientPort,
        db: AsyncSession,
    ) -> dict[str, str]:
        psp.verify_signature(payload, signature_header)
        event = psp.parse_event(payload)
        event_id = str(event["id"])
        event_type = str(event["type"])

        # Idempotency journal: first writer wins. DO NOTHING writes nothing on
        # conflict, so the duplicate path needs no rollback.
        result = cast(
            "CursorResult[Any]",
            await db.execute(
                pg_insert(StripeEvent)
                .values(event_id=event_id, status=EVENT_STATUS_SKIPPED)
                # PSP webhook journal keyed by the PSP event id
                .on_conflict_do_nothing(index_elements=[StripeEvent.event_id])
            ),
        )
        if not result.rowcount:
            logger.info("Webhook %s already processed; skipping", event_id)
            return {"status": "duplicate"}
        await db.flush()

        try:
            # SAVEPOINT: a permanent data error discards any partial apply but
            # keeps the journal row, which is then marked failed.
            async with db.begin_nested():
                outcome = await cls._apply(event_type, event, db, psp=psp)
        except WebhookDataError as exc:
            logger.warning(
                "Webhook %s (%s) cannot apply: %s", event_id, event_type, exc
            )
            outcome = _Outcome(EVENT_STATUS_FAILED, str(exc)[:1000])
        except Exception:
            # Transient failure: let the UoW roll back everything, including
            # the journal entry; the PSP retries and we reprocess from scratch.
            logger.exception("Webhook %s failed; transaction rolled back", event_id)
            raise
        await cls._mark(event_id, outcome.status, outcome.note, db)
        return {"status": _RESPONSE_STATUS[outcome.status]}

    @classmethod
    async def _mark(
        cls, event_id: str, event_status: str, note: str | None, db: AsyncSession
    ) -> None:
        # One UPDATE (was SELECT the journal row + flush its UPDATE).
        await db.execute(
            update(StripeEvent)
            .where(StripeEvent.event_id == event_id)
            .values(status=event_status, note=note)
            .execution_options(synchronize_session=False)
        )

    # --- Intent gate (ADR-0003 G1) ----------------------------------------------

    @classmethod
    async def _intent_gate(
        cls,
        obj: dict[str, Any],
        customer: BillingCustomer,
        *,
        event_id: str,
        session_id: str | None,
        db: AsyncSession,
    ) -> tuple[_Outcome | None, OperationIntent | None]:
        """``(None, intent)`` when the object may apply; else the ``failed``
        outcome. ``intent`` is ``None`` for an object without the marker.

        Only objects carrying our intent marker are gated (legacy / operator
        objects apply as before). The intent must be ``issued``, belong to the
        event's customer and — for a checkout session — name this session.
        Otherwise the completion is refused (:meth:`_refuse_completion`).
        """
        raw = _as_dict(obj.get("metadata")).get(INTENT_METADATA_KEY)
        if raw is None:
            return None, None
        intent: OperationIntent | None = None
        try:
            intent_id = uuid.UUID(str(raw))
        except ValueError:
            intent_id = None
        if intent_id is not None:
            # Owner-scoped: the id comes from PSP metadata, so it is looked
            # up only among the intents of the event's PSP customer. Another
            # tenant's intent reads as "unknown" and is never touched.
            found = await db.execute(
                select(OperationIntent)
                .where(
                    OperationIntent.id == intent_id,
                    OperationIntent.customer_id == customer.id,
                )
                .with_for_update()
            )
            intent = found.scalar_one_or_none()
        if (
            intent is not None
            and intent.status == INTENT_ISSUED
            # Only a CHECKOUT intent authorizes a purchase / new subscription.
            and intent.operation == OPERATION_CHECKOUT
            # A checkout completion must name the session issued for it.
            and (session_id is None or intent.psp_session_id == session_id)
        ):
            return None, intent

        state = "unknown" if intent is None else intent.status
        refused = await cls._refuse_completion(
            str(raw),
            intent,
            customer,
            state=state,
            event_id=event_id,
            session_id=session_id,
            db=db,
        )
        return refused, None

    @classmethod
    async def _refuse_completion(
        cls,
        raw_intent_id: str,
        intent: OperationIntent | None,
        customer: BillingCustomer,
        *,
        state: str,
        event_id: str,
        session_id: str | None,
        db: AsyncSession,
    ) -> _Outcome:
        """A completion the intent does not authorize: NOT applied — journaled
        ``failed``, audited (required) and the intent flagged ``followup_at``
        for an operator, once per intent (the money may have moved; refunding
        is a manual operator step). ``followup_at`` is not a status change: an
        issued intent stays issued."""
        first_report = True
        if intent is not None:
            flagged = await db.execute(
                update(OperationIntent)
                .where(
                    OperationIntent.id == intent.id,
                    OperationIntent.customer_id == customer.id,
                    OperationIntent.followup_at.is_(None),
                )
                .values(followup_at=datetime.now(UTC))
                .returning(OperationIntent.id)
                .execution_options(synchronize_session=False)
            )
            first_report = flagged.scalar_one_or_none() is not None
        if first_report:
            record_audit(
                AUDIT_UNAUTHORIZED_COMPLETION,
                actor_id=None,
                actor_type="system",
                resource_type="billing_intent",
                resource_id=raw_intent_id[:255],
                data={
                    "intent_status": state,
                    "event_id": event_id,
                    "psp_session_id": session_id,
                    "customer_id": str(customer.id),
                },
                required=True,
            )
        logger.error(
            "Webhook %s: completion for intent %s in state %r was NOT applied; "
            "operator follow-up required (refund is manual)",
            event_id,
            raw_intent_id,
            state,
        )
        return _Outcome(
            EVENT_STATUS_FAILED,
            f"completion for {state} intent {raw_intent_id}: not applied, "
            "operator follow-up",
        )

    @classmethod
    async def _subscription_mismatch(
        cls,
        intent: OperationIntent,
        items: list[Any],
        price_id: int | None,
        db: AsyncSession,
    ) -> str | None:
        """Why an issued checkout intent does NOT authorize this new
        subscription (ADR-0003 open item 16), or ``None``.

        The intent authorized ONE subscription to ONE price: the subscription
        must have exactly one item, priced at the intent's catalog price, and
        no other subscription may already have been created for the intent
        (also a partial UNIQUE index on ``billing_subscriptions.intent_id``,
        so two concurrent deliveries cannot both insert)."""
        if len(items) != 1 or price_id is None or price_id != intent.price_id:
            return "price mismatch"
        # Billing creates every checkout line item with quantity 1 (a missing
        # quantity is read as 1); more seats are not what was authorized.
        quantity = _as_dict(items[0]).get("quantity", 1)
        if quantity != 1 or isinstance(quantity, bool):
            return "quantity mismatch"
        existing = await db.execute(
            select(BillingSubscription.id).where(
                BillingSubscription.intent_id == intent.id
            )
        )
        if existing.first() is not None:
            return "intent already yielded a subscription"
        return None

    # --- Appliers -------------------------------------------------------------

    @classmethod
    async def _apply(
        cls,
        event_type: str,
        event: dict[str, Any],
        db: AsyncSession,
        *,
        psp: StripeClientPort | None = None,
    ) -> _Outcome:
        data_object = _as_dict(_as_dict(event.get("data")).get("object"))

        if event_type in _SUBSCRIPTION_EVENTS:
            return await cls._apply_subscription(
                event_type, event, data_object, db, psp=psp
            )
        if event_type in _CHECKOUT_EVENTS:
            return await cls._apply_checkout(
                event_type, data_object, db, event_id=str(event.get("id", ""))
            )
        if event_type == INVOICE_PAID:
            return await cls._apply_invoice_paid(event, data_object, db)
        if event_type == CHARGE_REFUNDED:
            return await cls._apply_charge_refunded(event, data_object, db)
        if event_type in _DISPUTE_EVENTS:
            return await cls._apply_dispute(event_type, event, data_object, db)

        logger.info("Webhook event type %s ignored", event_type)
        return _skipped(f"event type {event_type} not handled")

    @classmethod
    async def _find_customer_by_external_id(
        cls, external_id: str | None, db: AsyncSession
    ) -> BillingCustomer | None:
        if not external_id:
            return None
        result = await db.execute(
            select(BillingCustomer).where(
                BillingCustomer.stripe_customer_id == external_id
            )
        )
        return result.scalar_one_or_none()

    @classmethod
    async def _require_customer(
        cls, obj: dict[str, Any], db: AsyncSession
    ) -> BillingCustomer:
        external_id = _ref_id(obj.get("customer"))
        customer = await cls._find_customer_by_external_id(external_id, db)
        if customer is None:
            raise WebhookDataError(f"unknown PSP customer {external_id!r}")
        return customer

    @classmethod
    async def _lock_subscription(
        cls, stripe_sub_id: str, db: AsyncSession
    ) -> BillingSubscription | None:
        # Row lock serialises concurrent deliveries for one subscription so the
        # ordering guard compares against the truly latest applied event.
        result = await db.execute(
            select(BillingSubscription)
            .where(BillingSubscription.stripe_subscription_id == stripe_sub_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _is_stale(sub: BillingSubscription, created: int | None) -> bool:
        if sub.last_event_at is None:
            return False
        if created is None:
            return True  # cannot be ordered against known history
        return created < sub.last_event_at

    @staticmethod
    def _advance(sub: BillingSubscription, event_id: str, created: int | None) -> None:
        if created is not None and (
            sub.last_event_at is None or created > sub.last_event_at
        ):
            sub.last_event_at = created
        sub.last_event_id = event_id

    @classmethod
    async def _refetch_subscription(
        cls, psp: StripeClientPort | None, stripe_sub_id: str
    ) -> dict[str, Any] | None:
        """The PSP's CURRENT subscription object (optional client capability)."""
        retrieve = getattr(psp, "retrieve_subscription", None)
        if retrieve is None:
            return None
        try:
            fresh = await retrieve(stripe_sub_id)
        except Exception as exc:  # noqa: BLE001 - fall back to delivery order
            logger.warning(
                "subscription %s: re-fetch on a same-second tie failed (%s); "
                "applying in delivery order",
                stripe_sub_id,
                type(exc).__name__,
            )
            return None
        if not isinstance(fresh, dict) or _ref_id(fresh.get("id")) != stripe_sub_id:
            return None
        return fresh

    @classmethod
    async def _apply_subscription(  # noqa: C901
        cls,
        event_type: str,
        event: dict[str, Any],
        sub_obj: dict[str, Any],
        db: AsyncSession,
        *,
        psp: StripeClientPort | None = None,
    ) -> _Outcome:
        customer = await cls._require_customer(sub_obj, db)
        stripe_sub_id = _ref_id(sub_obj.get("id"))
        if stripe_sub_id is None:
            raise WebhookDataError("subscription object has no id")
        event_id = str(event["id"])
        created = _event_created(event)

        sub = await cls._lock_subscription(stripe_sub_id, db)
        is_new = sub is None
        if sub is not None:
            if sub.customer_id != customer.id:
                raise WebhookDataError(
                    f"subscription {stripe_sub_id} belongs to another customer"
                )
            if sub.is_terminal:
                return _skipped(f"subscription already {sub.status} (terminal)")
            if event_type == SUBSCRIPTION_CREATED:
                return _skipped("created event for an already-known subscription")
            # A deletion is final whenever it arrives; anything else must be
            # at least as new as the last applied event.
            if event_type != SUBSCRIPTION_DELETED and cls._is_stale(sub, created):
                return _skipped(
                    f"stale event (created={created}, last={sub.last_event_at})"
                )
            if (
                event_type != SUBSCRIPTION_DELETED
                and created is not None
                and created == sub.last_event_at
            ):
                # Same-second tie: timestamps cannot order the two events;
                # prefer the PSP's current state over this payload.
                fresh = await cls._refetch_subscription(psp, stripe_sub_id)
                if fresh is not None:
                    if _ref_id(fresh.get("customer")) != _ref_id(
                        sub_obj.get("customer")
                    ):
                        raise WebhookDataError(
                            f"subscription {stripe_sub_id}: re-fetched object "
                            "belongs to another customer"
                        )
                    sub_obj = fresh

        intent: OperationIntent | None = None
        if is_new:
            # A NEW subscription is the activation: only for an issued intent.
            refused, intent = await cls._intent_gate(
                sub_obj, customer, event_id=event_id, session_id=None, db=db
            )
            if refused is not None:
                return refused

        status_value = sub_obj.get("status")
        if event_type == SUBSCRIPTION_DELETED:
            status_value = STATUS_CANCELED
        if not isinstance(status_value, str) or not status_value:
            raise WebhookDataError(f"subscription {stripe_sub_id} has no status")
        if status_value not in PSP_SUBSCRIPTION_STATUSES:
            raise WebhookDataError(
                f"subscription {stripe_sub_id}: unknown status {status_value!r}"
            )

        # Resolve the local price by the PSP price id. When the event carries
        # items, their price IS the subscription's price: an id the catalog does
        # not know becomes NULL — never the previous (possibly higher) price —
        # and grants nothing (fail closed, ADR-0003 open item 11). The PSP state
        # is still stored; an audit record flags it for an operator.
        price_known = True
        price_id: int | None = None
        unknown_price: str | None = None
        items = _as_list(_as_dict(sub_obj.get("items")).get("data"))
        if items:
            stripe_price_id = _ref_id(_as_dict(items[0]).get("price"))
            if stripe_price_id:
                price_result = await db.execute(
                    select(BillingPrice.id).where(
                        BillingPrice.stripe_price_id == stripe_price_id
                    )
                )
                price_id = price_result.scalar_one_or_none()
            if price_id is None:
                price_known = False
                unknown_price = stripe_price_id or "<missing>"

        if intent is not None:
            mismatch = await cls._subscription_mismatch(intent, items, price_id, db)
            if mismatch is not None:
                return await cls._refuse_completion(
                    str(intent.id),
                    intent,
                    customer,
                    state=f"{intent.status} ({mismatch})",
                    event_id=event_id,
                    session_id=None,
                    db=db,
                )

        period_end_ts = subscription_period_end(sub_obj)
        if period_end_ts is None and status_value in _PAYING_STATUSES:
            logger.error(
                "subscription %s (status=%s) has no current_period_end in either "
                "API shape; it will not grant access",
                stripe_sub_id,
                status_value,
            )

        previous = (
            None if sub is None else (sub.price_id, bool(sub.cancel_at_period_end))
        )
        if sub is None:
            # Origin decides GRANTING, never syncing: an issued intent passed
            # the gate above (marker present) -> "intent"; no marker -> "psp"
            # (operator-created / migrated / restored; grants only under
            # BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS).
            has_marker = (
                _as_dict(sub_obj.get("metadata")).get(INTENT_METADATA_KEY) is not None
            )
            sub = BillingSubscription(
                customer_id=customer.id,
                stripe_subscription_id=stripe_sub_id,
                status=status_value,
                origin=ORIGIN_INTENT if has_marker else ORIGIN_PSP,
                intent_id=intent.id if intent is not None else None,
            )
            db.add(sub)
        sub.status = status_value
        if price_id is not None or not price_known:
            sub.price_id = price_id
        sub.current_period_end = BillingService.parse_ts(period_end_ts)
        sub.trial_end = BillingService.parse_ts(sub_obj.get("trial_end"))
        sub.canceled_at = BillingService.parse_ts(sub_obj.get("canceled_at"))
        sub.cancel_at_period_end = bool(sub_obj.get("cancel_at_period_end", False))
        cls._advance(sub, event_id, created)
        await db.flush()
        if (
            previous is not None
            and event_type == SUBSCRIPTION_UPDATED
            and previous != (sub.price_id, bool(sub.cancel_at_period_end))
        ):
            await cls._audit_unattributed_change(
                sub, customer, previous, event_id=event_id, created=created, db=db
            )
        if not price_known:
            record_audit(
                AUDIT_UNKNOWN_PRICE,
                actor_id=None,
                actor_type="system",
                resource_type="billing_subscription",
                resource_id=stripe_sub_id,
                data={
                    "customer_id": str(customer.id),
                    "psp_price_id": unknown_price,
                    "status": status_value,
                    "event_id": event_id,
                },
                required=True,
            )
            logger.error(
                "subscription %s: PSP price %r is not in the catalog; state "
                "stored, NO entitlements granted (operator follow-up)",
                stripe_sub_id,
                unknown_price,
            )

        if status_value == STATUS_CANCELED:
            channel = events.SUBSCRIPTION_CANCELED_V1
        elif is_new:
            channel = events.SUBSCRIPTION_CREATED_V1
        else:
            channel = events.SUBSCRIPTION_UPDATED_V1
        await events.billing_event(
            channel,
            customer.id,
            customer.tenant_type,
            customer.tenant_id,
            {
                "status": sub.status,
                "stripe_subscription_id": stripe_sub_id,
                "cancel_at_period_end": sub.cancel_at_period_end,
                # Additive within .v1: whether THIS subscription grants
                # entitlements (catalog price, intent policy, period).
                # Consumers must use it, never derive access from status.
                "grants_access": _grants(sub),
                # Additive within .v1: the catalog plan (or null).
                "plan": await _plan(sub, db),
            },
        ).publish()
        return _processed()

    @classmethod
    async def _apply_checkout(  # noqa: C901
        cls,
        event_type: str,
        session_obj: dict[str, Any],
        db: AsyncSession,
        *,
        event_id: str = "",
    ) -> _Outcome:
        """One-time credit purchases (credit packs)."""
        if session_obj.get("mode") != "payment":
            return _skipped("subscription checkout (handled by subscription events)")
        session_id = _ref_id(session_obj.get("id"))
        if session_id is None:
            raise WebhookDataError("checkout session has no id")
        customer = await cls._require_customer(session_obj, db)
        metadata = _as_dict(session_obj.get("metadata"))

        meta_customer = metadata.get("customer_id")
        if meta_customer is None:
            raise WebhookDataError(
                f"checkout {session_id}: metadata.customer_id is missing"
            )
        if str(meta_customer) != str(customer.id):
            raise WebhookDataError(
                f"checkout {session_id}: metadata customer does not match the "
                "session's PSP customer"
            )
        try:
            local_price_id = int(str(metadata.get("price_id")))
        except (TypeError, ValueError):
            raise WebhookDataError(
                f"checkout {session_id}: metadata.price_id missing or invalid"
            ) from None

        price_result = await db.execute(
            select(BillingPrice, BillingProduct)
            .join(BillingProduct, BillingProduct.id == BillingPrice.product_id)
            .where(BillingPrice.id == local_price_id)
        )
        row = price_result.one_or_none()
        if row is None:
            raise WebhookDataError(
                f"checkout {session_id}: unknown price {local_price_id}"
            )
        price, product = row
        if product.type != PRODUCT_TYPE_ONE_TIME or not price.credits:
            return _skipped(f"price {local_price_id} grants no credits")
        credits = int(price.credits)
        cls._ensure_amount_matches(session_id, session_obj, price)

        if event_type == CHECKOUT_ASYNC_FAILED:
            logger.warning(
                "checkout %s: async payment failed; no credits granted", session_id
            )
            return _processed(f"async payment failed for checkout {session_id}")
        payment_status = session_obj.get("payment_status")
        if payment_status != "paid":
            return _skipped(
                f"checkout {session_id} not paid yet "
                f"(payment_status={payment_status!r})"
            )

        refused, intent = await cls._intent_gate(
            session_obj,
            customer,
            event_id=event_id,
            session_id=session_id,
            db=db,
        )
        if refused is not None:
            return refused
        if intent is not None and intent.price_id != local_price_id:
            # The session's metadata names another price than the intent was
            # authorized for (ADR-0003 open item 16).
            return await cls._refuse_completion(
                str(intent.id),
                intent,
                customer,
                state=f"{intent.status} (price mismatch)",
                event_id=event_id,
                session_id=session_id,
                db=db,
            )

        # Lock order, identical in the refund / dispute path: the payment's
        # STATE row (created-or-locked first: INSERT ... ON CONFLICT DO UPDATE
        # waits for a concurrent, still uncommitted insert of the same row by
        # a refund / dispute, so this transaction then sees what that one
        # recorded — a plain SELECT ... FOR UPDATE would miss the uncommitted
        # row under READ COMMITTED and both could commit without the reversal;
        # it also bumps updated_at, so the orphan purge's re-check skips a row
        # a completion is binding), then the GRANT row (inserted here), then
        # the LEDGER / balance (add_credits, reverse_credits).
        payment_intent = _ref_id(session_obj.get("payment_intent"))
        if payment_intent is None:
            # Credits are reversible only through the payment intent (refunds
            # and disputes name it, never the session): a grant without one
            # could never be reversed. Grant nothing; journaled failed (not
            # raised, so the audit and the flag survive), operator follow-up.
            return await cls._refuse_grant(
                session_id, customer, intent, event_id=event_id, db=db
            )
        state = await cls._lock_state(payment_intent, db, create=True)
        # The link a refund / dispute needs (it names the payment intent,
        # never the session); same transaction as the grant, once per session.
        inserted = await db.execute(
            pg_insert(CreditGrant)
            .values(
                customer_id=customer.id,
                psp_session_id=session_id,
                psp_payment_intent_id=payment_intent,
                price_id=local_price_id,
                credits=credits,
                amount=int(price.amount),
                currency=str(price.currency).lower(),
            )
            # Both the session and payment-intent keys are unique. An event
            # naming a new session for an already-granted payment must not
            # abort the transaction or credit the customer a second time.
            # UNIQUE session key on the grant row itself
            .on_conflict_do_nothing()
            .returning(CreditGrant.id)
        )
        new_grant = inserted.scalar_one_or_none() is not None
        if not new_grant:
            existing = (
                await db.execute(
                    select(CreditGrant).where(
                        CreditGrant.psp_payment_intent_id == payment_intent
                    )
                )
            ).scalar_one_or_none()
            if existing is None or existing.psp_session_id != session_id:
                raise WebhookDataError(
                    "Payment was already granted for another session"
                )
            return _processed("payment already granted")
        # Same operation key for completed + async_payment_succeeded: whichever
        # arrives first grants, the other is an idempotent replay.
        try:
            await BillingService.add_credits(
                customer,
                credits,
                f"checkout:{session_id}",
                db,
                reason=f"checkout purchase (price {local_price_id})",
            )
        except ValueError as exc:
            raise WebhookDataError(f"checkout {session_id}: {exc}") from exc
        if new_grant and state is not None:  # noqa: SIM102 - the comment documents the inner condition
            # A refund / dispute delivered BEFORE this completion was recorded
            # on the payment's state: apply it now, in the grant's transaction.
            # (A completion arriving more than PAYMENT_STATE_RETENTION after
            # an orphan refund / dispute finds it purged — documented limit.)
            if state.amount_refunded or state.dispute_status is not None:
                await cls._converge_payment(
                    state,
                    event_id,
                    f"early refund/dispute of {session_id}",
                    db,
                    strict=False,
                )
        return _processed()

    @classmethod
    async def _refuse_grant(
        cls,
        session_id: str,
        customer: BillingCustomer,
        intent: OperationIntent | None,
        *,
        event_id: str,
        db: AsyncSession,
    ) -> _Outcome:
        """A paid credit-pack completion WITHOUT a payment intent: nothing is
        granted (it could never be reversed), audited (required), ERROR
        logged, and its intent (if any) flagged ``followup_at``."""
        if intent is not None:
            await db.execute(
                update(OperationIntent)
                .where(
                    OperationIntent.id == intent.id,
                    OperationIntent.customer_id == customer.id,
                    OperationIntent.followup_at.is_(None),
                )
                .values(followup_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
        record_audit(
            AUDIT_GRANT_REFUSED,
            actor_id=None,
            actor_type="system",
            resource_type="billing_customer",
            resource_id=str(customer.id),
            data={
                "reason": "paid checkout without payment_intent",
                "psp_session_id": session_id,
                "event_id": event_id,
                "intent_id": str(intent.id) if intent is not None else None,
            },
            required=True,
        )
        logger.error(
            "checkout %s: paid without a payment_intent; NO credits granted "
            "(they could never be reversed); operator follow-up",
            session_id,
        )
        return _Outcome(
            EVENT_STATUS_FAILED,
            f"checkout {session_id}: paid without payment_intent, nothing granted",
        )

    # --- Portal attribution (ADR-0003 open item 10) ----------------------------

    @classmethod
    async def _audit_unattributed_change(
        cls,
        sub: BillingSubscription,
        customer: BillingCustomer,
        previous: tuple[int | None, bool],
        *,
        event_id: str,
        created: int | None,
        db: AsyncSession,
    ) -> None:
        """Audit a plan / ``cancel_at_period_end`` change for which the
        customer has no portal intent issued within the attribution window.

        Never blocks: the PSP's state is the truth and is stored regardless
        (blocking would desynchronize billing). Stripe does not tell which
        portal session (if any) made a change, so this is an ATTRIBUTION
        heuristic for operators, not an authorization: a change made in a
        portal session of this customer that was issued in the window is
        indistinguishable from one made by an operator in the same window.
        """
        at = (
            datetime.fromtimestamp(created, tz=UTC)
            if created is not None
            else datetime.now(UTC)
        )
        window = timedelta(minutes=settings.billing_portal_attribution_window_minutes)
        found = await db.execute(
            select(OperationIntent.id)
            .where(
                OperationIntent.customer_id == customer.id,
                OperationIntent.operation == OPERATION_CUSTOMER_PORTAL,
                OperationIntent.status == INTENT_ISSUED,
                OperationIntent.issued_at >= at - window,
                OperationIntent.issued_at <= at + _ATTRIBUTION_SKEW,
            )
            .limit(1)
        )
        if found.first() is not None:
            return
        record_audit(
            AUDIT_UNATTRIBUTED_CHANGE,
            actor_id=None,
            actor_type="system",
            resource_type="billing_subscription",
            resource_id=sub.stripe_subscription_id,
            data={
                "customer_id": str(customer.id),
                "event_id": event_id,
                "price_id_before": previous[0],
                "price_id_after": sub.price_id,
                "cancel_at_period_end_before": previous[1],
                "cancel_at_period_end_after": bool(sub.cancel_at_period_end),
                "window_minutes": settings.billing_portal_attribution_window_minutes,
            },
            required=True,
        )
        logger.warning(
            "subscription %s: plan / cancellation change with no portal intent "
            "of its customer in the last %d min (audited, applied)",
            sub.stripe_subscription_id,
            settings.billing_portal_attribution_window_minutes,
        )

    # --- Refunds / disputes (ADR-0003 open item 12) ---------------------------

    @classmethod
    async def _lock_grant(
        cls, payment_intent: str | None, db: AsyncSession
    ) -> CreditGrant | None:
        if not payment_intent:
            return None
        result = await db.execute(
            select(CreditGrant)
            .where(CreditGrant.psp_payment_intent_id == payment_intent)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    @classmethod
    async def _lock_state(
        cls, payment_intent: str, db: AsyncSession, *, create: bool
    ) -> PspPaymentState | None:
        """The payment's state row, locked (created first when ``create``).
        Lock order everywhere: state row, then grant row, then the ledger.

        The upsert bumps ``updated_at`` on every touch: the orphan purge's
        ``updated_at < cutoff`` is re-checked against the row version left by
        a concurrent toucher (READ COMMITTED re-evaluation after the lock
        wait), so it never deletes a row a completion is binding."""
        if create:
            # One statement: DO UPDATE locks the row (new or existing) and
            # RETURNING hands it back — no separate SELECT ... FOR UPDATE.
            upsert = (
                pg_insert(PspPaymentState)
                .values(psp_payment_intent_id=payment_intent)
                .on_conflict_do_update(
                    index_elements=[PspPaymentState.psp_payment_intent_id],
                    set_={"updated_at": func.clock_timestamp()},
                )
                .returning(PspPaymentState)
            )
            locked = await db.execute(
                select(PspPaymentState).from_statement(upsert),
                execution_options={"populate_existing": True},
            )
            state: PspPaymentState = locked.scalar_one()
            return state
        result = await db.execute(
            select(PspPaymentState)
            .where(PspPaymentState.psp_payment_intent_id == payment_intent)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    @classmethod
    async def _apply_charge_refunded(
        cls, event: dict[str, Any], charge: dict[str, Any], db: AsyncSession
    ) -> _Outcome:
        """Record the payment's cumulative refunded amount; reverse the
        grant's credits in proportion (rounded UP) — now, or when the grant
        is created if the refund arrived first."""
        charge_id = _ref_id(charge.get("id")) or "?"
        payment_intent = _ref_id(charge.get("payment_intent"))
        if payment_intent is None:
            return _skipped(f"refund of charge {charge_id}: no payment intent")
        refunded = charge.get("amount_refunded")
        if isinstance(refunded, bool) or not isinstance(refunded, int) or refunded < 0:
            raise WebhookDataError(f"refund of charge {charge_id}: bad amounts")
        payer = _ref_id(charge.get("customer"))
        if payer is None:
            # Ownership needs the charge's PSP customer (like invoice.paid).
            raise WebhookDataError(f"refund of charge {charge_id}: no PSP customer")
        currency = str(charge.get("currency") or "").lower() or None
        if currency is None:
            # The currency check needs it (like the customer).
            raise WebhookDataError(f"refund of charge {charge_id}: no currency")
        state = await cls._lock_state(payment_intent, db, create=True)
        assert state is not None  # noqa: S101 - type narrowing of an invariant
        if state.psp_customer_id not in (None, payer) or state.currency not in (
            None,
            currency,
        ):
            raise WebhookDataError(
                f"refund of charge {charge_id}: another customer / currency than "
                "recorded for its payment"
            )
        state.psp_customer_id = payer
        state.currency = currency
        state.amount_refunded = max(int(state.amount_refunded), refunded)
        return await cls._converge_payment(
            state, str(event["id"]), f"refund of {charge_id}", db, strict=True
        )

    @classmethod
    async def _apply_dispute(
        cls,
        event_type: str,
        event: dict[str, Any],
        dispute: dict[str, Any],
        db: AsyncSession,
    ) -> _Outcome:
        """Record the payment's dispute state. While a dispute is open or
        lost, the DISPUTED share of the credits is reversed (a partial dispute
        a proportional share); won restores it; an inquiry (``warning_*``, no
        funds withdrawn) reverses nothing — audit only. Subscription access is
        not changed by a dispute (ADR-0003 item 12)."""
        dispute_id = _ref_id(dispute.get("id")) or "?"
        payment_intent = _ref_id(dispute.get("payment_intent"))
        status_value = str(dispute.get("status") or "")
        target = _DISPUTE_STATUS.get(status_value)
        if payment_intent is None or target is None:
            return _skipped(f"dispute {dispute_id}: status {status_value!r} ignored")
        amount = dispute.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise WebhookDataError(f"dispute {dispute_id}: bad amount")
        state = await cls._lock_state(payment_intent, db, create=True)
        assert state is not None  # noqa: S101 - type narrowing of an invariant
        grant = await cls._lock_grant(payment_intent, db)
        if target == DISPUTE_INQUIRY or (
            grant is None and event_type == DISPUTE_CREATED
        ):
            cls._audit_dispute(
                dispute_id, dispute, event, inquiry=target == DISPUTE_INQUIRY
            )
        currency = str(dispute.get("currency") or "").lower() or None
        if currency is None:
            raise WebhookDataError(f"dispute {dispute_id}: no currency")
        if state.currency not in (None, currency):
            raise WebhookDataError(f"dispute {dispute_id}: currency mismatch")
        state.currency = currency
        if state.dispute_status in DISPUTE_TERMINAL:
            # A closed dispute never reopens (late / duplicate deliveries).
            await db.flush()
            return _skipped(f"dispute {dispute_id}: already {state.dispute_status}")
        if target == DISPUTE_INQUIRY and state.dispute_status not in (
            None,
            DISPUTE_INQUIRY,
        ):
            await db.flush()
            return _skipped(f"dispute {dispute_id}: inquiry after a dispute")
        state.dispute_status = target
        state.dispute_amount = amount
        if target == DISPUTE_INQUIRY:
            await db.flush()
            return _skipped(f"dispute {dispute_id}: inquiry (no funds withdrawn)")
        return await cls._converge_payment(
            state, str(event["id"]), f"dispute {dispute_id}", db, strict=True
        )

    @staticmethod
    def _audit_dispute(
        dispute_id: str,
        dispute: dict[str, Any],
        event: dict[str, Any],
        *,
        inquiry: bool,
    ) -> None:
        record_audit(
            AUDIT_CHARGE_DISPUTED,
            actor_id=None,
            actor_type="system",
            resource_type="psp_dispute",
            resource_id=dispute_id,
            data={
                "event_id": str(event.get("id", "")),
                "inquiry": inquiry,
                "status": dispute.get("status"),
                "charge": _ref_id(dispute.get("charge")),
                "payment_intent": _ref_id(dispute.get("payment_intent")),
                "amount": dispute.get("amount"),
                "currency": dispute.get("currency"),
                "reason": dispute.get("reason"),
            },
            required=True,
        )
        logger.error(
            "dispute %s (%s): operator follow-up", dispute_id, dispute.get("status")
        )

    @classmethod
    async def _converge_payment(
        cls,
        state: PspPaymentState,
        event_id: str,
        cause: str,
        db: AsyncSession,
        *,
        strict: bool,
    ) -> _Outcome:
        """Apply the payment's recorded state to its grant, if one exists.

        Without a grant the state stays recorded (``processed``): the grant's
        creation applies it in the same transaction. ``strict``: an owner /
        currency mismatch is a permanent data error (journaled ``failed``);
        otherwise (at grant creation) it is audited and nothing is reversed.
        """
        grant = await cls._lock_grant(state.psp_payment_intent_id, db)
        if grant is None:
            await db.flush()
            return _processed(f"{cause}: recorded; no credit grant for its payment")
        customer = await db.get(BillingCustomer, grant.customer_id)
        if customer is None:  # pragma: no cover - FK
            raise WebhookDataError(f"{cause}: grant customer is gone")
        mismatch = state.psp_customer_id not in (
            None,
            customer.stripe_customer_id,
        ) or state.currency not in (None, grant.currency)
        if mismatch:
            if strict:
                raise WebhookDataError(
                    f"{cause}: the payment's customer / currency is not the grant's"
                )
            record_audit(
                AUDIT_REVERSAL_REFUSED,
                actor_id=None,
                actor_type="system",
                resource_type="billing_customer",
                resource_id=str(customer.id),
                data={
                    "cause": cause,
                    "event_id": event_id,
                    "psp_session_id": grant.psp_session_id,
                },
                required=True,
            )
            logger.error("%s: payment state names another customer; ignored", cause)
            return _processed()
        return await cls._converge_grant(grant, state, customer, event_id, cause, db)

    @classmethod
    async def _converge_grant(
        cls,
        grant: CreditGrant,
        state: PspPaymentState,
        customer: BillingCustomer,
        event_id: str,
        cause: str,
        db: AsyncSession,
    ) -> _Outcome:
        """Move the grant's reversed credits to :func:`reversal_due` with ONE
        append-only ledger entry keyed by the PSP event id (durable dedupe,
        same transaction). Out-of-order / repeated events converge."""
        delta = reversal_due(grant, state) - int(grant.reversed)
        if delta == 0:
            await db.flush()
            return _skipped(f"{cause}: credits already reconciled")
        reason = f"{cause} (checkout {grant.psp_session_id})"
        key = f"{'reversal' if delta > 0 else 'restore'}:{event_id}"
        if await BillingService._ledger_has(customer.id, key, db):
            # Already written for this event: `reversed` is not moved again.
            await db.flush()
            return _skipped(f"{cause}: ledger entry {key} already applied")
        if delta > 0:
            balance = await BillingService.reverse_credits(
                customer, delta, key, db, reason=reason
            )
            if balance is None:  # pragma: no cover - checked just above
                return _skipped(f"{cause}: ledger entry {key} already applied")
            action = AUDIT_CREDITS_REVERSED
        else:
            balance = await BillingService.add_credits(
                customer,
                -delta,
                key,
                db,
                reason=reason,
                channel=events.CREDITS_RESTORED_V1,
            )
            action = AUDIT_CREDITS_RESTORED
        grant.reversed = int(grant.reversed) + delta
        await db.flush()
        record_audit(
            action,
            actor_id=None,
            actor_type="system",
            resource_type="billing_customer",
            resource_id=str(customer.id),
            data={
                "credits": abs(delta),
                "balance": balance,
                "cause": cause,
                "event_id": event_id,
                "psp_session_id": grant.psp_session_id,
            },
            required=True,
        )
        return _processed()

    @staticmethod
    def _ensure_amount_matches(
        session_id: str, session_obj: dict[str, Any], price: BillingPrice
    ) -> None:
        """The session's ``amount_total`` + ``currency`` must equal the price."""
        amount_total = session_obj.get("amount_total")
        currency = session_obj.get("currency")
        if isinstance(amount_total, bool) or not isinstance(amount_total, int):
            raise WebhookDataError(f"checkout {session_id}: amount_total is missing")
        if not isinstance(currency, str) or not currency:
            raise WebhookDataError(f"checkout {session_id}: currency is missing")
        if (
            amount_total != int(price.amount)
            or currency.lower() != str(price.currency).lower()
        ):
            raise WebhookDataError(
                f"checkout {session_id}: paid {amount_total} {currency.upper()} "
                f"but price {price.id} is {price.amount} "
                f"{str(price.currency).upper()}"
            )

    @classmethod
    async def _apply_invoice_paid(
        cls, event: dict[str, Any], invoice_obj: dict[str, Any], db: AsyncSession
    ) -> _Outcome:
        """Recover a past_due/unpaid subscription after a successful charge.

        Never resurrects a canceled subscription, never flips ``trialing``
        (a $0 trial invoice is "paid" too), and honours the ordering guard.
        """
        stripe_sub_id = invoice_subscription_id(invoice_obj)
        if not stripe_sub_id:
            return _skipped("invoice is not linked to a subscription")
        sub = await cls._lock_subscription(stripe_sub_id, db)
        if sub is None:
            return _skipped(f"unknown subscription {stripe_sub_id}")
        # Ownership: the invoice's PSP customer must own the subscription it
        # names (found by id alone otherwise).
        payer = await cls._find_customer_by_external_id(
            _ref_id(invoice_obj.get("customer")), db
        )
        if payer is None or payer.id != sub.customer_id:
            raise WebhookDataError(
                f"invoice for subscription {stripe_sub_id} is not from its customer"
            )
        if sub.is_terminal:
            return _skipped(f"subscription already {sub.status} (terminal)")
        created = _event_created(event)
        if cls._is_stale(sub, created):
            return _skipped(
                f"stale event (created={created}, last={sub.last_event_at})"
            )

        previous_status = sub.status
        status_changed = previous_status in RECOVERABLE_STATUSES
        if status_changed:
            sub.status = STATUS_ACTIVE

        period_moved = False
        period_end = BillingService.parse_ts(invoice_period_end(invoice_obj))
        if period_end is not None and (
            sub.current_period_end is None or period_end > sub.current_period_end
        ):
            sub.current_period_end = period_end
            period_moved = True

        if not (status_changed or period_moved):
            return _skipped(f"no state change (status={previous_status})")
        cls._advance(sub, str(event["id"]), created)
        await db.flush()

        if status_changed:
            customer_result = await db.execute(
                select(BillingCustomer).where(BillingCustomer.id == sub.customer_id)
            )
            customer = customer_result.scalar_one()
            await events.billing_event(
                events.SUBSCRIPTION_UPDATED_V1,
                customer.id,
                customer.tenant_type,
                customer.tenant_id,
                {
                    "status": sub.status,
                    "stripe_subscription_id": stripe_sub_id,
                    "grants_access": _grants(sub),
                    "plan": await _plan(sub, db),
                },
            ).publish()
        return _processed()


#: Payment-state rows that never matched a credit grant (refunds / disputes
#: of subscription invoices, pre-0008 purchases) are kept this long, then
#: purged by billing's housekeeping task. A completion arrives within days of
#: its payment, so an orphan this old can never be matched any more.
#: Limit: a completion (or replay) arriving later than this after an orphan
#: refund / dispute finds the early state purged and grants without the
#: reversal. Every touch of a state row bumps ``updated_at`` (the upsert in
#: ``_lock_state``), so the purge never deletes a row a concurrent
#: completion is binding.
PAYMENT_STATE_RETENTION = timedelta(days=90)


async def purge_orphan_payment_states(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """Delete payment-state rows older than :data:`PAYMENT_STATE_RETENTION`
    with no credit grant for their payment; returns how many."""
    cutoff = (now or datetime.now(UTC)) - PAYMENT_STATE_RETENTION
    result = await db.execute(
        delete(PspPaymentState)
        .where(
            PspPaymentState.updated_at < cutoff,
            ~exists().where(
                CreditGrant.psp_payment_intent_id
                == PspPaymentState.psp_payment_intent_id
            ),
        )
        .execution_options(synchronize_session=False)
    )
    return int(cast("CursorResult[Any]", result).rowcount or 0)
