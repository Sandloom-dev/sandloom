"""PSP integration port (BILLING_SPEC §5, IStripeClient).

The port keeps the billing service testable and provider-replaceable. Two
implementations ship with the framework:

- FakeStripeClient — deterministic, dependency-free (tests / local dev)
- StripeHttpClient — real Stripe REST API via httpx (no SDK dependency)
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


class StripeSignatureError(ValueError):
    """Webhook signature verification failed."""


@dataclass(frozen=True, slots=True)
class PspSession:
    """A PSP-hosted session: its id (to expire / match webhooks), URL and —
    for a checkout session — when the PSP expires it (``None``: unknown)."""

    id: str
    url: str
    expires_at: datetime | None = None


#: Checkout session statuses (Stripe): payable / paid / no longer payable.
CHECKOUT_OPEN = "open"
CHECKOUT_COMPLETE = "complete"
CHECKOUT_EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CheckoutSessionInfo:
    """What the reconciler needs to know about an existing checkout session."""

    id: str
    status: str
    metadata: dict[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None


@runtime_checkable
class StripeClientPort(Protocol):
    """Operations billing needs from the PSP."""

    async def create_customer(
        self,
        *,
        email: str | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Create a PSP customer; return its external id."""
        ...

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
        """Create a checkout session; return its id and URL.

        ``metadata`` lands on the session; ``subscription_metadata`` on the
        subscription a ``subscription``-mode session creates (Stripe:
        ``subscription_data[metadata]``), so its webhooks carry it. The same
        ``idempotency_key`` returns the same session.
        """
        ...

    async def expire_checkout_session(self, session_id: str) -> None:
        """Expire an open checkout session: it can no longer be paid.

        Raises when the PSP refuses (e.g. the session was already completed —
        the webhook gate then decides).
        """
        ...

    async def list_checkout_sessions(
        self, *, customer_id: str, created_gte: int
    ) -> list[CheckoutSessionInfo]:
        """The checkout sessions of PSP customer ``customer_id`` created at or
        after ``created_gte`` (unix seconds), any status.

        Used by the intent reconciler (#72) to find a session whose id billing
        never learned (the PSP answer was lost) by the intent marker in its
        metadata. Raises on a PSP failure (the caller retries later).
        """
        ...

    async def create_billing_portal_session(
        self,
        *,
        customer_id: str,
        return_url: str,
        idempotency_key: str | None = None,
    ) -> str:
        """Create a customer-portal session; return its URL.

        There is no expire counterpart: Stripe cannot expire a portal session
        (its URL is short-lived); billing never hands out a URL it no longer
        authorizes (ADR-0003 G1).
        """
        ...

    def verify_signature(self, payload: bytes, signature_header: str) -> None:
        """Raise StripeSignatureError when the webhook signature is invalid."""
        ...

    def parse_event(self, payload: bytes) -> dict[str, Any]:
        """Parse a verified webhook payload into an event dict."""
        ...


def verify_stripe_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> None:
    """Verify Stripe webhook signature (t=...,v1=... HMAC-SHA256 scheme)."""
    if not signature_header:
        raise StripeSignatureError("Missing Stripe-Signature header")

    timestamp: str | None = None
    candidates: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)

    if timestamp is None or not candidates:
        raise StripeSignatureError("Malformed Stripe-Signature header")

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise StripeSignatureError("Invalid timestamp in signature") from exc

    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance_seconds:
        raise StripeSignatureError("Signature timestamp outside tolerance")

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    # Compare BYTES: a candidate from the header may carry non-ASCII
    # (latin-1 decoded) characters, on which compare_digest(str, str) raises.
    expected_bytes = expected.encode()
    if not any(
        hmac.compare_digest(expected_bytes, c.encode("utf-8", "surrogatepass"))
        for c in candidates
    ):
        raise StripeSignatureError("Signature mismatch")


def sign_stripe_payload(
    payload: bytes, secret: str, timestamp: int | None = None
) -> str:
    """Produce a valid Stripe-Signature header (used by tests and tooling)."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def parse_stripe_event(payload: bytes) -> dict[str, Any]:
    """Decode a webhook payload; raise ValueError on malformed JSON."""
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed webhook payload: {exc}") from exc
    if not isinstance(event, dict) or "id" not in event or "type" not in event:
        raise ValueError("Webhook payload missing id/type")
    return event


def epoch_to_datetime(value: object) -> datetime | None:
    """A PSP unix timestamp (seconds) as an aware UTC datetime, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)
