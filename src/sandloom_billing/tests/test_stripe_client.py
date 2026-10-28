"""StripeHttpClient: version pinning, retries and the circuit breaker."""

from typing import Any

import httpx
import pytest

from sandloom_billing.psp import STRIPE_API_VERSION, StripeApiError, StripeHttpClient
from sandloom_billing.psp.stripe_http import _CircuitBreaker


class _Resp:
    def __init__(self, status_code: int = 200, body: dict[str, Any] | None = None):
        self.status_code = status_code
        self.text = ""
        self._body = body or {"id": "cus_1", "url": "https://x"}

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr("sandloom_billing.psp.stripe_http.asyncio.sleep", _sleep)


async def test_requests_pin_api_version_and_idempotency_key() -> None:
    client = StripeHttpClient("sk_test")
    seen: dict[str, str] = {}

    async def _send(path: str, data: dict, headers: dict) -> Any:
        seen.update(headers)
        return _Resp()

    client._send = _send  # type: ignore[method-assign]
    await client.create_customer(idempotency_key="k1")
    assert seen["Stripe-Version"] == STRIPE_API_VERSION
    assert seen["Idempotency-Key"] == "k1"


async def test_client_error_is_not_retried() -> None:
    client = StripeHttpClient("sk_test")
    calls = 0

    async def _send(path: str, data: dict, headers: dict) -> Any:
        nonlocal calls
        calls += 1
        return _Resp(status_code=400)

    client._send = _send  # type: ignore[method-assign]
    with pytest.raises(StripeApiError, match="Stripe error 400"):
        await client.create_customer()
    assert calls == 1


@pytest.mark.parametrize("status_code", [409, 429])
async def test_transient_client_status_preserves_idempotency_and_retries(status_code):
    client = StripeHttpClient("sk_test")
    calls = []

    async def send(path, data, headers):
        calls.append(dict(headers))
        return _Resp(status_code=status_code if len(calls) == 1 else 200)

    client._send = send
    await client.create_customer(idempotency_key="retry-safe-operation")
    assert len(calls) == 2
    assert calls[0]["Idempotency-Key"] == calls[1]["Idempotency-Key"]


async def test_provider_error_text_is_never_in_exception_or_intent_reason():
    client = StripeHttpClient("sk_test")
    response = _Resp(
        status_code=400, body={"error": {"code": "card_declined", "type": "card_error"}}
    )
    response.text = '{"email":"private@example.com","secret":"sensitive"}'

    async def send(path, data, headers):
        return response

    client._send = send
    with pytest.raises(StripeApiError) as exc:
        await client.create_customer()
    assert "private@example.com" not in str(exc.value)
    assert "sensitive" not in str(exc.value)
    assert "card_declined" in str(exc.value)


def test_half_open_admits_a_single_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(
        "sandloom_billing.psp.stripe_http.time.monotonic", lambda: clock[0]
    )
    breaker = _CircuitBreaker(threshold=2, cooldown_seconds=30)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open and not breaker.allow()

    clock[0] += 31
    assert breaker.allow() is True  # the probe
    assert breaker.allow() is False  # everyone else still fails fast
    assert breaker.allow() is False

    breaker.record_failure()  # probe failed: re-open for a full cooldown
    assert not breaker.allow()
    clock[0] += 31
    assert breaker.allow() is True
    breaker.record_success()  # probe succeeded: closed
    assert breaker.allow() and breaker.allow()


async def test_retries_stop_once_breaker_opens() -> None:
    client = StripeHttpClient("sk_test", max_retries=3)
    client._breaker = _CircuitBreaker(threshold=1)
    calls = 0

    async def _send(path: str, data: dict, headers: dict) -> Any:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    client._send = _send  # type: ignore[method-assign]
    with pytest.raises(StripeApiError):
        await client.create_customer()
    assert calls == 1
    with pytest.raises(StripeApiError, match="circuit breaker is open"):
        await client.create_customer()
    assert calls == 1


async def test_retrieve_subscription_is_a_versioned_get() -> None:
    client = StripeHttpClient("sk_test")
    seen: dict[str, Any] = {}

    async def _send_get(path: str, headers: dict) -> Any:
        seen["path"], seen["headers"] = path, headers
        return _Resp(body={"id": "sub_1", "status": "active"})

    client._send_get = _send_get  # type: ignore[method-assign]
    sub = await client.retrieve_subscription("sub_1")
    assert sub == {"id": "sub_1", "status": "active"}
    assert seen["path"] == "/subscriptions/sub_1"
    assert seen["headers"]["Stripe-Version"] == STRIPE_API_VERSION
    assert "Idempotency-Key" not in seen["headers"]


async def test_checkout_returns_the_session_id_and_tags_the_subscription() -> None:
    """ADR-0003 G1: the session id is needed to expire it; the intent marker
    must reach the subscription the checkout creates (its webhooks)."""
    client = StripeHttpClient("sk_test")
    sent: dict[str, Any] = {}

    async def _send(path: str, data: dict, headers: dict) -> Any:
        sent.update(path=path, data=data, headers=headers)
        return _Resp(body={"id": "cs_1", "url": "https://checkout/cs_1"})

    client._send = _send  # type: ignore[method-assign]
    session = await client.create_checkout_session(
        customer_id="cus_1",
        price_external_id="price_1",
        success_url="https://s",
        cancel_url="https://c",
        metadata={"sandloom_intent_id": "i-1"},
        subscription_metadata={"sandloom_intent_id": "i-1"},
        idempotency_key="sandloom:intent:i-1",
    )
    assert (session.id, session.url) == ("cs_1", "https://checkout/cs_1")
    assert sent["data"]["metadata[sandloom_intent_id]"] == "i-1"
    assert sent["data"]["subscription_data[metadata][sandloom_intent_id]"] == "i-1"
    assert sent["headers"]["Idempotency-Key"] == "sandloom:intent:i-1"


async def test_expire_checkout_session_posts_to_the_expire_endpoint() -> None:
    client = StripeHttpClient("sk_test")
    paths: list[str] = []

    async def _send(path: str, data: dict, headers: dict) -> Any:
        paths.append(path)
        return _Resp(body={"id": "cs_1", "status": "expired"})

    client._send = _send  # type: ignore[method-assign]
    await client.expire_checkout_session("cs_1")
    assert paths == ["/checkout/sessions/cs_1/expire"]


async def test_a_rejected_request_is_definitive() -> None:
    """A 4xx is ``StripeRequestRejected`` (the intent fails); anything else
    stays an ambiguous ``StripeApiError`` (the intent stays pending)."""
    from sandloom_billing.psp import StripeRequestRejected

    client = StripeHttpClient("sk_test")

    async def _send(path: str, data: dict, headers: dict) -> Any:
        return _Resp(status_code=400)

    client._send = _send  # type: ignore[method-assign]
    with pytest.raises(StripeRequestRejected):
        await client.expire_checkout_session("cs_done")


async def test_checkout_session_carries_its_expiry() -> None:
    client = StripeHttpClient("sk_test")

    async def _send(path: str, data: dict, headers: dict) -> Any:
        return _Resp(
            body={"id": "cs_1", "url": "https://x", "expires_at": 1_900_000_000}
        )

    client._send = _send  # type: ignore[method-assign]
    session = await client.create_checkout_session(
        customer_id="cus_1",
        price_external_id="price_1",
        success_url="https://s",
        cancel_url="https://c",
    )
    assert session.expires_at is not None
    assert int(session.expires_at.timestamp()) == 1_900_000_000


async def test_list_checkout_sessions_filters_by_customer_and_pages() -> None:
    """The reconciler's lookup (#72): by PSP customer + created window, paged
    with ``starting_after``, metadata kept for the marker match."""
    client = StripeHttpClient("sk_test")
    paths: list[str] = []
    pages = [
        {
            "data": [
                {
                    "id": "cs_a",
                    "status": "open",
                    "metadata": {"sandloom_intent_id": "i1"},
                    "expires_at": 1_900_000_000,
                }
            ],
            "has_more": True,
        },
        {
            "data": [{"id": "cs_b", "status": "expired", "metadata": {}}],
            "has_more": False,
        },
    ]

    async def _send_get(path: str, headers: dict) -> Any:
        paths.append(path)
        return _Resp(body=pages[len(paths) - 1])

    client._send_get = _send_get  # type: ignore[method-assign]
    found = await client.list_checkout_sessions(customer_id="cus_1", created_gte=123)

    assert [s.id for s in found] == ["cs_a", "cs_b"]
    assert (
        found[0].metadata == {"sandloom_intent_id": "i1"} and found[0].status == "open"
    )
    assert "customer=cus_1" in paths[0] and "created%5Bgte%5D=123" in paths[0]
    assert "starting_after=cs_a" in paths[1]
