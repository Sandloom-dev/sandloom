"""Deterministic in-memory PSP client for tests and local development."""

import time
import uuid
from typing import Any

from .port import (
    CHECKOUT_COMPLETE,
    CHECKOUT_OPEN,
    CheckoutSessionInfo,
    PspSession,
    StripeSignatureError,
    epoch_to_datetime,
    parse_stripe_event,
    verify_stripe_signature,
)

#: Stripe's default checkout session lifetime (24 h).
SESSION_LIFETIME_SECONDS = 24 * 3600


class FakeStripeClient:
    """Implements StripeClientPort without network access.

    Checkout/portal URLs are synthesized; created customers are recorded so
    tests can assert against them.

    Webhooks are fail-closed: without a webhook secret every webhook is
    REJECTED (a forged event would otherwise grant credits/subscriptions).
    Local tests sign their payloads with
    :func:`sandloom_billing.psp.sign_stripe_payload`.
    """

    def __init__(self, webhook_secret: str | None = None):
        self._webhook_secret = webhook_secret
        self.created_customers: list[dict[str, Any]] = []
        self.checkout_sessions: list[dict[str, Any]] = []
        self.portal_sessions: list[dict[str, Any]] = []
        #: Ids of checkout sessions expired via :meth:`expire_checkout_session`.
        self.expired_sessions: list[str] = []
        #: Current PSP-side subscription objects, by id (tests seed it) —
        #: served by :meth:`retrieve_subscription`.
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self._customers_by_idempotency_key: dict[str, str] = {}
        self._checkout_by_idempotency_key: dict[str, PspSession] = {}
        self._portal_by_idempotency_key: dict[str, str] = {}

    async def create_customer(
        self,
        *,
        email: str | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key and idempotency_key in self._customers_by_idempotency_key:
            return self._customers_by_idempotency_key[idempotency_key]

        external_id = f"cus_fake_{uuid.uuid4().hex[:16]}"
        self.created_customers.append(
            {
                "id": external_id,
                "email": email,
                "metadata": metadata or {},
                "idempotency_key": idempotency_key,
            }
        )
        if idempotency_key:
            self._customers_by_idempotency_key[idempotency_key] = external_id
        return external_id

    async def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_external_id: str,
        success_url: str,
        cancel_url: str,
        mode: str = "subscription",
        metadata: dict[str, str] | None = None,
        subscription_metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> PspSession:
        if idempotency_key and idempotency_key in self._checkout_by_idempotency_key:
            return self._checkout_by_idempotency_key[idempotency_key]

        session_id = f"cs_fake_{uuid.uuid4().hex[:16]}"
        url = f"https://checkout.stripe.local/c/{session_id}"
        created = int(time.time())
        expires_at = created + SESSION_LIFETIME_SECONDS
        self.checkout_sessions.append(
            {
                "id": session_id,
                "customer": customer_id,
                "price": price_external_id,
                "mode": mode,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata or {},
                "subscription_metadata": (
                    (subscription_metadata or {}) if mode == "subscription" else {}
                ),
                "url": url,
                "status": CHECKOUT_OPEN,
                "created": created,
                "expires_at": expires_at,
                "idempotency_key": idempotency_key,
            }
        )
        session = PspSession(
            id=session_id, url=url, expires_at=epoch_to_datetime(expires_at)
        )
        if idempotency_key:
            self._checkout_by_idempotency_key[idempotency_key] = session
        return session

    async def expire_checkout_session(self, session_id: str) -> None:
        for session in self.checkout_sessions:
            if session["id"] == session_id:
                if session["status"] == CHECKOUT_COMPLETE:
                    raise RuntimeError(f"checkout session {session_id} is complete")
                session["status"] = "expired"
                self.expired_sessions.append(session_id)
                return
        raise LookupError(f"no such checkout session {session_id!r}")

    async def list_checkout_sessions(
        self, *, customer_id: str, created_gte: int
    ) -> list[CheckoutSessionInfo]:
        return [
            CheckoutSessionInfo(
                id=s["id"],
                status=s["status"],
                metadata=dict(s["metadata"]),
                expires_at=epoch_to_datetime(s["expires_at"]),
            )
            for s in self.checkout_sessions
            if s["customer"] == customer_id and s["created"] >= created_gte
        ]

    async def create_billing_portal_session(
        self,
        *,
        customer_id: str,
        return_url: str,
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key and idempotency_key in self._portal_by_idempotency_key:
            return self._portal_by_idempotency_key[idempotency_key]

        session_id = f"bps_fake_{uuid.uuid4().hex[:16]}"
        url = f"https://billing.stripe.local/p/{session_id}"
        self.portal_sessions.append(
            {
                "id": session_id,
                "customer": customer_id,
                "return_url": return_url,
                "url": url,
                "idempotency_key": idempotency_key,
            }
        )
        if idempotency_key:
            self._portal_by_idempotency_key[idempotency_key] = url
        return url

    async def retrieve_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Current subscription object; ``LookupError`` when unknown."""
        try:
            return dict(self.subscriptions[subscription_id])
        except KeyError:
            raise LookupError(f"no such subscription {subscription_id!r}") from None

    def verify_signature(self, payload: bytes, signature_header: str) -> None:
        # With a secret the fake enforces the real Stripe signature scheme.
        if self._webhook_secret:
            verify_stripe_signature(payload, signature_header, self._webhook_secret)
            return
        raise StripeSignatureError(
            "Webhook secret is not configured (STRIPE_WEBHOOK_SECRET); "
            "unsigned webhooks are rejected"
        )

    def parse_event(self, payload: bytes) -> dict[str, Any]:
        return parse_stripe_event(payload)
