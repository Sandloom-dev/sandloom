"""PSP webhook endpoint (public, signature-protected)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from sandloom.sdk.web import DbSession, public
from sandloom_billing.api.billing import get_psp
from sandloom_billing.psp import StripeClientPort, StripeSignatureError
from sandloom_billing.services import WebhookService

#: ADR-0003 class F (system): no user subject — the PSP signature and the
#: durable event dedupe authorize it. Marked always-strict like every class-F
#: route (a no-op here: no bearer token is read).
webhooks_router = APIRouter(
    prefix="/api/v1/billing/webhooks",
    tags=["billing"],
    dependencies=[
        public(reason="PSP webhook; the Stripe signature is verified in the endpoint"),
    ],
)


@webhooks_router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: DbSession,
    psp: StripeClientPort = Depends(get_psp),
) -> dict[str, str]:
    """Verify, dedupe, and apply a Stripe webhook event."""
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        return await WebhookService.handle_stripe_webhook(payload, signature, psp, db)
    except StripeSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid webhook signature: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
