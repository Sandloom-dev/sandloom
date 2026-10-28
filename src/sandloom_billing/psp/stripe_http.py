"""Stripe REST API client via httpx (no SDK dependency).

Covers exactly the surface billing needs. Retries with exponential backoff
and a simple circuit breaker per BILLING_SPEC §12.
"""

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .port import (
    CheckoutSessionInfo,
    PspSession,
    StripeSignatureError,
    epoch_to_datetime,
    parse_stripe_event,
    verify_stripe_signature,
)

logger = logging.getLogger("sandloom_billing.stripe")

_STRIPE_API = "https://api.stripe.com/v1"

#: Stripe API version every request is pinned to. The webhook parser targets
#: this shape (``basil``: ``current_period_end`` on subscription items, invoice
#: subscription under ``parent.subscription_details``) AND the legacy shape,
#: because webhook payloads follow the endpoint's configured version, not this
#: header. Bump deliberately, together with the parser.
STRIPE_API_VERSION = "2025-03-31.basil"


#: Upper bound of pages read by :meth:`StripeHttpClient.list_checkout_sessions`.
_LIST_MAX_PAGES = 10


class StripeApiError(RuntimeError):
    """Stripe API returned an error or is unreachable."""


class StripeRequestRejected(StripeApiError):
    """A 4xx answer: the request is wrong, retrying cannot help (definitive).

    Every other :class:`StripeApiError` (5xx, network, open breaker) is
    AMBIGUOUS: the PSP may or may not have acted.
    """


_StripeClientError = StripeRequestRejected


def _error_tags(response: httpx.Response) -> str:
    """Only bounded machine codes may leave a provider error response."""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        return ""
    return " ".join(
        value
        for key in ("type", "code")
        if isinstance(value := error.get(key), str)
        and re.fullmatch(r"[a-z_]{1,80}", value)
    )


class _CircuitBreaker:
    """Open after N consecutive failures; half-open after cooldown.

    Half-open admits exactly ONE probe: concurrent callers keep failing fast
    until the probe succeeds (closed) or fails (re-opened for another cooldown).
    """

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 30.0):
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._probe_in_flight:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._probe_in_flight = True  # the single half-open probe
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._probe_in_flight or self._failures >= self._threshold:
            self._opened_at = time.monotonic()
        self._probe_in_flight = False


class StripeHttpClient:
    """Implements StripeClientPort against the live Stripe API."""

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str | None = None,
        *,
        max_retries: int = 3,
        timeout_seconds: float = 10.0,
    ):
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._breaker = _CircuitBreaker()
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                trust_env=False,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http

    async def aclose(self) -> None:
        """Release pooled connections at module/worker shutdown."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send(
        self, path: str, data: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        return await self._client().post(
            f"{_STRIPE_API}{path}",
            data=data,
            headers=headers,
            auth=(self._secret_key, ""),
        )

    async def _send_get(self, path: str, headers: dict[str, str]) -> httpx.Response:
        return await self._client().get(
            f"{_STRIPE_API}{path}",
            headers=headers,
            auth=(self._secret_key, ""),
        )

    async def _post(
        self, path: str, data: dict[str, Any], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            lambda headers: self._send(path, data, headers), idempotency_key
        )

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request(lambda headers: self._send_get(path, headers))

    async def _request(  # noqa: C901
        self,
        send: Callable[[dict[str, str]], Awaitable[httpx.Response]],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not self._breaker.allow():
            raise StripeApiError("Stripe circuit breaker is open")

        headers = {"Stripe-Version": STRIPE_API_VERSION}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await send(headers)
                if resp.status_code >= 500:
                    raise StripeApiError(f"Stripe 5xx: {resp.status_code}")
                if resp.status_code in {409, 429}:
                    # A busy/locked PSP is not a definitive rejection. Keep
                    # the same idempotency key and leave exhausted intents
                    # pending for reconciliation, never mark them failed.
                    raise StripeApiError(f"Stripe retryable status {resp.status_code}")
                if resp.status_code >= 400:
                    raise _StripeClientError(
                        f"Stripe error {resp.status_code}: {_error_tags(resp)}"
                    )
                result: dict[str, Any] = resp.json()
            except _StripeClientError:
                # The API answered: the circuit is healthy; do not retry a 4xx.
                self._breaker.record_success()
                raise
            except (httpx.HTTPError, StripeApiError, ValueError) as exc:
                last_error = exc
                self._breaker.record_failure()
                if attempt + 1 >= self._max_retries or self._breaker.is_open:
                    break  # out of attempts, or the breaker tripped: fail fast
                backoff = (2**attempt) + random.random()  # noqa: S311 - retry jitter, not a secret
                logger.warning(
                    "Stripe call failed (attempt %d/%d): %s; retry in %.1fs",
                    attempt + 1,
                    self._max_retries,
                    type(exc).__name__,
                    backoff,
                )
                await asyncio.sleep(backoff)
            else:
                self._breaker.record_success()
                return result

        raise StripeApiError(
            f"Stripe unreachable after retries: {type(last_error).__name__}"
        )

    async def create_customer(
        self,
        *,
        email: str | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        data: dict[str, Any] = {}
        if email:
            data["email"] = email
        for key, value in (metadata or {}).items():
            data[f"metadata[{key}]"] = value
        result = await self._post("/customers", data, idempotency_key)
        return str(result["id"])

    async def retrieve_subscription(self, subscription_id: str) -> dict[str, Any]:
        """The subscription's CURRENT state (used on same-second webhook ties)."""
        return await self._get(f"/subscriptions/{quote(subscription_id, safe='')}")

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
        data: dict[str, Any] = {
            "customer": customer_id,
            "mode": mode,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][price]": price_external_id,
            "line_items[0][quantity]": 1,
        }
        for key, value in (metadata or {}).items():
            data[f"metadata[{key}]"] = value
        if mode == "subscription":
            for key, value in (subscription_metadata or {}).items():
                data[f"subscription_data[metadata][{key}]"] = value
        result = await self._post("/checkout/sessions", data, idempotency_key)
        return PspSession(
            id=str(result["id"]),
            url=str(result["url"]),
            expires_at=epoch_to_datetime(result.get("expires_at")),
        )

    async def list_checkout_sessions(
        self, *, customer_id: str, created_gte: int
    ) -> list[CheckoutSessionInfo]:
        """``GET /v1/checkout/sessions?customer=…&created[gte]=…`` (paged).

        Stripe's search API does not cover checkout sessions, so the marker is
        matched by the caller on the listed sessions' metadata. Bounded to
        ``_LIST_MAX_PAGES`` pages of 100 (a customer with more sessions in the
        window is an anomaly: the caller sees only the newest ones).
        """
        found: list[CheckoutSessionInfo] = []
        starting_after: str | None = None
        for _ in range(_LIST_MAX_PAGES):
            params: dict[str, Any] = {
                "customer": customer_id,
                "created[gte]": created_gte,
                "limit": 100,
            }
            if starting_after:
                params["starting_after"] = starting_after
            page = await self._get(f"/checkout/sessions?{urlencode(params)}")
            rows = page.get("data") or []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                metadata = row.get("metadata") or {}
                found.append(
                    CheckoutSessionInfo(
                        id=str(row["id"]),
                        status=str(row.get("status") or ""),
                        metadata={str(k): str(v) for k, v in metadata.items()},
                        expires_at=epoch_to_datetime(row.get("expires_at")),
                    )
                )
            if not page.get("has_more") or not rows:
                break
            starting_after = str(rows[-1].get("id"))
        return found

    async def expire_checkout_session(self, session_id: str) -> None:
        """``POST /v1/checkout/sessions/{id}/expire`` (open sessions only)."""
        await self._post(
            f"/checkout/sessions/{quote(session_id, safe='')}/expire",
            {},
            # Expiring is idempotent at the PSP; the key only dedupes retries.
            f"sandloom:expire:{session_id}",
        )

    async def create_billing_portal_session(
        self,
        *,
        customer_id: str,
        return_url: str,
        idempotency_key: str | None = None,
    ) -> str:
        result = await self._post(
            "/billing_portal/sessions",
            {"customer": customer_id, "return_url": return_url},
            idempotency_key,
        )
        return str(result["url"])

    def verify_signature(self, payload: bytes, signature_header: str) -> None:
        if not self._webhook_secret:
            raise StripeSignatureError("STRIPE_WEBHOOK_SECRET is not configured")
        verify_stripe_signature(payload, signature_header, self._webhook_secret)

    def parse_event(self, payload: bytes) -> dict[str, Any]:
        return parse_stripe_event(payload)
