"""PSP (payment service provider) integration layer."""

from sandloom_billing.config import billing_settings

from .fake import FakeStripeClient
from .port import (
    CHECKOUT_COMPLETE,
    CHECKOUT_EXPIRED,
    CHECKOUT_OPEN,
    CheckoutSessionInfo,
    PspSession,
    StripeClientPort,
    StripeSignatureError,
    parse_stripe_event,
    sign_stripe_payload,
    verify_stripe_signature,
)
from .stripe_http import (
    STRIPE_API_VERSION,
    StripeApiError,
    StripeHttpClient,
    StripeRequestRejected,
)

__all__ = [
    "CHECKOUT_COMPLETE",
    "CHECKOUT_EXPIRED",
    "CHECKOUT_OPEN",
    "CheckoutSessionInfo",
    "PspSession",
    "StripeRequestRejected",
    "StripeClientPort",
    "StripeSignatureError",
    "StripeApiError",
    "FakeStripeClient",
    "StripeHttpClient",
    "STRIPE_API_VERSION",
    "verify_stripe_signature",
    "sign_stripe_payload",
    "parse_stripe_event",
    "build_psp_client",
    "install_psp_client",
    "active_psp_client",
]


def build_psp_client() -> StripeClientPort:
    """Build the PSP client according to billing's settings (fake by default)."""
    cfg = billing_settings()
    webhook_secret = (
        cfg.stripe_webhook_secret.get_secret_value()
        if cfg.stripe_webhook_secret
        else None
    )
    if cfg.billing_provider == "stripe":
        if not cfg.stripe_secret_key:
            raise RuntimeError("BILLING_PROVIDER=stripe requires STRIPE_SECRET_KEY")
        return StripeHttpClient(
            cfg.stripe_secret_key.get_secret_value(), webhook_secret
        )
    return FakeStripeClient(webhook_secret)


# ---------------------------------------------------------------------------
# The module-private PSP client.
#
# The PSP client (fake or Stripe) is billing's INTERNAL adapter: it creates
# checkout/portal sessions and verifies webhooks with billing's secrets. It is
# deliberately NOT a service-registry contract — a public registration would
# hand the full client to any module and, via SERVICE_BINDINGS, to any RPC
# caller. Billing's own endpoints reach it through ``active_psp_client()``.
# ---------------------------------------------------------------------------

_active_client: StripeClientPort | None = None


def install_psp_client(client: StripeClientPort | None) -> StripeClientPort | None:
    """Install billing's PSP client; returns the previous one (tests restore it)."""
    global _active_client
    previous = _active_client
    _active_client = client
    return previous


def active_psp_client() -> StripeClientPort:
    """Billing's PSP client (built from settings on first use)."""
    global _active_client
    if _active_client is None:
        _active_client = build_psp_client()
    return _active_client


async def close_psp_client() -> None:
    """Close the live HTTP pool without constructing an unused PSP client."""
    if isinstance(_active_client, StripeHttpClient):
        await _active_client.aclose()
