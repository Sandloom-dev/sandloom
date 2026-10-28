"""ADR-0003 G1, operation-authorization model for billing's class-F routes.

``checkout-session`` / ``customer-portal`` call an external PSP, which no
database transaction can roll back. The flow is:

1. authorize (live ``get_member_state``: role + membership ``version``);
2. persist an authorized **intent** in its own, committed transaction
   (``billing_operation_intents``: org, actor, operation, params digest,
   authorized version, status ``pending``);
3. call the PSP with the intent id as the PSP idempotency key (a retry of the
   same request reuses the intent, so it gets the SAME session — never a blind
   new call with a fresh key);
4. re-verify the authority (and the caller's session marks) and record the
   outcome in another short transaction: ``issued`` (URL returned) or
   ``revoked`` (URL withheld, checkout session EXPIRED at the PSP).

The PSP webhook only activates a subscription / grants credits for a session
whose intent is ``issued``; a completion for a revoked / unknown intent is
audited and flagged for operator follow-up instead.
"""

import asyncio
import contextvars
import json
import time
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.security import ensure_user_not_blocked
from sandloom_billing.models import (
    INTENT_ISSUED,
    INTENT_PENDING,
    INTENT_REVOKED,
    BillingCustomer,
    BillingSubscription,
    OperationIntent,
)
from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.services import BillingService
from sandloom_billing.tests import tenancy_api
from sandloom_billing.tests.conftest import WEBHOOK_SECRET, make_user_headers

pytestmark = pytest.mark.asyncio

CHECKOUT = "/api/v1/billing/checkout-session"
PORTAL = "/api/v1/billing/customer-portal"
WEBHOOK = "/api/v1/billing/webhooks/stripe"


def _idem(headers: dict, key: str | None = None) -> dict:
    return {**headers, "Idempotency-Key": key or f"intent-{uuid.uuid4().hex}"}


async def _fresh_login_headers(user_id: uuid.UUID) -> dict[str, str]:
    """Mint after the same live issuance check used by a real login.

    An independent request cannot inherit the revoking request's issuance
    floor. Reading the current mark before minting also handles its clock-skew
    margin, so these scenarios test authorization with a valid new session.
    """
    await ensure_user_not_blocked(user_id)
    _, headers = make_user_headers(user_id)
    return headers


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _intents(sessions: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessions() as db:
        rows = await db.execute(
            select(OperationIntent).order_by(OperationIntent.created_at)
        )
        return list(rows.scalars())


async def _audits(sessions: async_sessionmaker[AsyncSession], action: str) -> int:
    from sqlalchemy import text

    async with sessions() as db:
        count = await db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
        )
        return int(count.scalar_one())


#: Each test org's owner headers (the owner demotes the admin through
#: tenancy's public API).
_OWNERS: dict[uuid.UUID, dict[str, str]] = {}


async def _org(client: AsyncClient, test_db: AsyncSession) -> tuple[uuid.UUID, Any]:
    """A committed org whose admin may manage billing."""
    owner, admin = make_user_headers(), make_user_headers()
    org_id = await tenancy_api.create_org(client, owner[1], name="Intent Co")
    await tenancy_api.add_member(
        client, org_id, owner[1], admin[0], tenancy_api.ROLE_ADMIN
    )
    await test_db.commit()
    _OWNERS[org_id] = owner[1]
    return org_id, admin


async def _demote(
    client: AsyncClient, test_db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Demote ``user_id`` to member in tenancy and commit it."""
    await tenancy_api.change_role(
        client, org_id, _OWNERS[org_id], user_id, tenancy_api.ROLE_MEMBER
    )
    await test_db.commit()


def _demote_during(
    monkeypatch: pytest.MonkeyPatch,
    psp: Any,
    method: str,
    client: AsyncClient,
    test_db: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Commit a demotion of ``user_id`` INSIDE the PSP call."""
    original = getattr(psp, method)

    async def wrapped(**kwargs: Any) -> Any:
        # Another actor, concurrently: its own context, never the route's
        # (the route's PSP call holds no unit, spec §4.6).
        await asyncio.create_task(
            _demote(client, test_db, org_id, user_id), context=contextvars.Context()
        )
        return await original(**kwargs)

    monkeypatch.setattr(psp, method, wrapped)


# --------------------------------------------------------------------------- #
# Normal path
# --------------------------------------------------------------------------- #


async def test_checkout_persists_an_issued_intent_keyed_to_the_psp_session(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text

    [intent] = await _intents(sessions)
    [session] = fake_psp.checkout_sessions
    assert intent.status == INTENT_ISSUED
    assert intent.operation == "checkout"
    assert intent.actor_id == user_id
    assert intent.tenant_type == "user" and intent.tenant_id == user_id
    assert intent.price_id == catalog["pro_month"].id
    assert intent.psp_idempotency_key == f"sandloom:intent:{intent.id}"
    assert session["idempotency_key"] == intent.psp_idempotency_key
    assert intent.psp_session_id == session["id"]
    assert resp.json()["url"] == session["url"] == intent.psp_session_url
    # The intent travels with the session and the subscription it creates.
    assert session["metadata"]["sandloom_intent_id"] == str(intent.id)
    assert session["subscription_metadata"] == {"sandloom_intent_id": str(intent.id)}


async def test_org_checkout_records_the_authorized_membership_version(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )
    assert resp.status_code == 200, resp.text
    [intent] = await _intents(sessions)
    assert intent.tenant_type == "organization" and intent.tenant_id == org_id
    assert intent.actor_id == admin_id
    assert intent.authorized_version is not None


# --------------------------------------------------------------------------- #
# Authority lost during the PSP call
# --------------------------------------------------------------------------- #


async def test_role_loss_during_checkout_revokes_the_intent_and_expires_the_session(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    _demote_during(
        monkeypatch,
        fake_psp,
        "create_checkout_session",
        async_client,
        test_db,
        org_id,
        admin_id,
    )

    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )

    assert resp.status_code == 403, resp.text
    [session] = fake_psp.checkout_sessions
    assert session["url"] not in resp.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert intent.psp_session_id == session["id"]
    assert fake_psp.expired_sessions == [session["id"]]
    assert await _audits(sessions, "billing.intent.revoked") == 1


async def test_role_loss_during_portal_revokes_the_intent_and_withholds_the_url(
    async_client: AsyncClient,
    test_db: AsyncSession,
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Stripe cannot expire a portal session: it is never handed out."""
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    _demote_during(
        monkeypatch,
        fake_psp,
        "create_billing_portal_session",
        async_client,
        test_db,
        org_id,
        admin_id,
    )

    resp = await async_client.post(
        PORTAL, json={"org_id": str(org_id)}, headers=_idem(admin_headers)
    )

    assert resp.status_code == 403, resp.text
    [portal] = fake_psp.portal_sessions
    assert portal["url"] not in resp.text
    [intent] = await _intents(sessions)
    assert intent.operation == "customer_portal"
    assert intent.status == INTENT_REVOKED
    assert fake_psp.expired_sessions == []


async def test_block_during_personal_checkout_revokes_and_expires(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Personal context: the caller's own session (identity's revocation /
    block marks) is the authority, re-read before the intent is issued."""
    from sandloom.sdk.security import block_user
    from sandloom.sdk.testing import recording_uow

    user_id, headers = make_user_headers()
    original = fake_psp.create_checkout_session

    async def blocked_meanwhile(**kwargs: Any) -> Any:
        with recording_uow():
            await block_user(user_id, reason="fraud review", actor_id=None)
        return await original(**kwargs)

    monkeypatch.setattr(fake_psp, "create_checkout_session", blocked_meanwhile)
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )

    assert resp.status_code == 401, resp.text
    [session] = fake_psp.checkout_sessions
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert fake_psp.expired_sessions == [session["id"]]


# --------------------------------------------------------------------------- #
# PSP timeout, then a retry
# --------------------------------------------------------------------------- #


async def test_psp_timeout_then_retry_reuses_the_intent_and_the_psp_session(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The PSP created the session but the answer was lost: the intent stays
    ``pending`` and the retry (same Idempotency-Key) reconciles with the SAME
    PSP idempotency key — the same session, not a second one."""
    _, headers = make_user_headers()
    original = fake_psp.create_checkout_session
    calls: list[str | None] = []

    async def lost_answer(**kwargs: Any) -> Any:
        calls.append(kwargs.get("idempotency_key"))
        result = await original(**kwargs)
        if len(calls) == 1:
            raise TimeoutError("PSP answer lost")
        return result

    monkeypatch.setattr(fake_psp, "create_checkout_session", lost_answer)
    body = {"price_id": catalog["pro_month"].id}
    request_headers = _idem(headers, "timeout-then-retry")

    first = await async_client.post(CHECKOUT, json=body, headers=request_headers)
    assert first.status_code == 503, first.text
    [pending] = await _intents(sessions)
    assert pending.status == INTENT_PENDING

    retry = await async_client.post(CHECKOUT, json=body, headers=request_headers)
    assert retry.status_code == 200, retry.text

    [intent] = await _intents(sessions)
    assert intent.id == pending.id and intent.status == INTENT_ISSUED
    assert calls == [f"sandloom:intent:{intent.id}"] * 2
    [session] = fake_psp.checkout_sessions  # no second session
    assert retry.json()["url"] == session["url"]


async def test_a_definitive_psp_rejection_fails_the_intent(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A 4xx from the PSP cannot succeed on retry: the intent is ``failed``
    (502), which frees the request key — the next attempt is a new intent."""
    from sandloom_billing.models import INTENT_FAILED
    from sandloom_billing.psp import StripeRequestRejected

    _, headers = make_user_headers()
    original = fake_psp.create_checkout_session
    attempts: list[int] = []

    async def reject_once(**kwargs: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise StripeRequestRejected("Stripe error 400: no such price")
        return await original(**kwargs)

    monkeypatch.setattr(fake_psp, "create_checkout_session", reject_once)
    body = {"price_id": catalog["pro_month"].id}
    request_headers = _idem(headers, "rejected-then-retry")

    first = await async_client.post(CHECKOUT, json=body, headers=request_headers)
    assert first.status_code == 502, first.text
    [failed] = await _intents(sessions)
    assert failed.status == INTENT_FAILED

    retry = await async_client.post(CHECKOUT, json=body, headers=request_headers)
    assert retry.status_code == 200, retry.text
    statuses = sorted(i.status for i in await _intents(sessions))
    assert statuses == sorted([INTENT_FAILED, INTENT_ISSUED])


async def test_a_new_request_key_gets_a_new_intent(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = make_user_headers()
    body = {"price_id": catalog["pro_month"].id}
    for key in ("first-key", "second-key"):
        resp = await async_client.post(CHECKOUT, json=body, headers=_idem(headers, key))
        assert resp.status_code == 200, resp.text
    assert len(await _intents(sessions)) == 2
    assert len(fake_psp.checkout_sessions) == 2


# --------------------------------------------------------------------------- #
# Webhook gating
# --------------------------------------------------------------------------- #


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET)}


def _subscription_created(
    customer: str, intent_id: str | None, sub_id: str | None = None
) -> dict:
    metadata = {"sandloom_intent_id": intent_id} if intent_id else {}
    return {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "customer.subscription.created",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": sub_id or f"sub_{uuid.uuid4().hex[:12]}",
                "customer": customer,
                "status": "active",
                "current_period_end": int(time.time()) + 86400 * 30,
                "cancel_at_period_end": False,
                "metadata": metadata,
                "items": {"data": [{"price": {"id": "price_pro_month"}}]},
            }
        },
    }


async def _psp_customer(sessions: async_sessionmaker[AsyncSession]) -> str:
    async with sessions() as db:
        external = await db.execute(
            select(BillingCustomer.stripe_customer_id).where(
                BillingCustomer.stripe_customer_id.is_not(None)
            )
        )
        return str(external.scalar_one())


async def _subscriptions(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as db:
        count = await db.execute(select(func.count()).select_from(BillingSubscription))
        return int(count.scalar_one())


async def _revoked_checkout(
    async_client: AsyncClient,
    test_db: AsyncSession,
    price_id: int,
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> Any:
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    _demote_during(
        monkeypatch,
        fake_psp,
        "create_checkout_session",
        async_client,
        test_db,
        org_id,
        admin_id,
    )
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": price_id},
        headers=_idem(admin_headers),
    )
    assert resp.status_code == 403, resp.text
    [intent] = await _intents(sessions)
    return intent


async def test_subscription_for_a_revoked_intent_is_not_activated(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent = await _revoked_checkout(
        async_client,
        test_db,
        catalog["pro_month"].id,
        fake_psp,
        monkeypatch,
        sessions,
    )
    body, headers = _signed(
        _subscription_created(await _psp_customer(sessions), str(intent.id))
    )

    resp = await async_client.post(WEBHOOK, content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    await test_db.flush()
    assert await _count_in(test_db, BillingSubscription) == 0
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1
    flagged = await test_db.get(OperationIntent, intent.id, populate_existing=True)
    assert flagged is not None and flagged.followup_at is not None
    assert flagged.status == INTENT_REVOKED


async def test_credit_purchase_for_a_revoked_intent_grants_nothing(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent = await _revoked_checkout(
        async_client,
        test_db,
        catalog["pack_price"].id,
        fake_psp,
        monkeypatch,
        sessions,
    )
    [session] = fake_psp.checkout_sessions
    payload = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": session["id"],
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": catalog["pack_price"].amount,
                "currency": "usd",
                "customer": session["customer"],
                "metadata": session["metadata"],
            }
        },
    }
    body, headers = _signed(payload)

    resp = await async_client.post(WEBHOOK, content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    customer = await test_db.get(BillingCustomer, intent.customer_id)
    assert customer is not None
    assert await BillingService.get_credit_balance(customer.id, test_db) == 0
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1


async def test_subscription_for_an_unknown_intent_is_not_activated(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200
    body, sig = _signed(
        _subscription_created(await _psp_customer(sessions), str(uuid.uuid4()))
    )

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "failed"
    assert await _count_in(test_db, BillingSubscription) == 0
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1


async def test_subscription_for_an_issued_intent_activates(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200
    [intent] = await _intents(sessions)
    body, sig = _signed(
        _subscription_created(await _psp_customer(sessions), str(intent.id))
    )

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "processed", resp.text
    assert await _count_in(test_db, BillingSubscription) == 1


async def test_session_created_before_intents_still_activates(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Backward compatibility: a subscription without the intent marker (a
    checkout created before this change, or by an operator) applies as before."""
    _, headers = make_user_headers()
    await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    body, sig = _signed(_subscription_created(await _psp_customer(sessions), None))

    resp = await async_client.post(WEBHOOK, content=body, headers=sig)

    assert resp.json()["status"] == "processed", resp.text
    assert await _count_in(test_db, BillingSubscription) == 1


async def _count_in(db: AsyncSession, model: Any) -> int:
    count = await db.execute(select(func.count()).select_from(model))
    return int(count.scalar_one())


async def _audit_in(db: AsyncSession, action: str) -> int:
    from sqlalchemy import text

    count = await db.execute(
        text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
    )
    return int(count.scalar_one())


# --------------------------------------------------------------------------- #
# The authority owner unavailable at the re-check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "outage",
    ["remote_unavailable", "provider_crash"],
)
@pytest.mark.sandloom_allow_server_errors  # asserts the masked 500 itself
async def test_owner_unreachable_at_the_recheck_revokes_and_expires(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    outage: str,
) -> None:
    """Tenancy answers at authorize, then cannot answer the re-check (RPC
    peer unreachable -> 503, or a provider failure): fail closed — the intent
    is not issued (revoked), the checkout session is expired, no URL."""
    from sandloom.sdk.errors import RemoteServiceUnavailable
    from sandloom_billing.services import tenant_resolver

    org_id, (_, admin_headers) = await _org(async_client, test_db)
    original = tenant_resolver._member_state
    calls: list[int] = []

    async def flaky(membership: Any, org: Any, user: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return await original(membership, org, user)
        if outage == "remote_unavailable":
            raise RemoteServiceUnavailable("tenancy.membership.v1", reason="timeout")
        raise ConnectionError("tenancy database unreachable")

    monkeypatch.setattr(tenant_resolver, "_member_state", flaky)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )

    assert resp.status_code in (500, 503), resp.text
    [session] = fake_psp.checkout_sessions
    assert session["url"] not in resp.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert fake_psp.expired_sessions == [session["id"]]


async def test_owner_unreachable_at_authorize_creates_nothing(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.sdk.errors import RemoteServiceUnavailable
    from sandloom_billing.services import tenant_resolver

    org_id, (_, admin_headers) = await _org(async_client, test_db)

    async def down(*_: Any) -> Any:
        raise RemoteServiceUnavailable("tenancy.membership.v1", reason="unreachable")

    monkeypatch.setattr(tenant_resolver, "_member_state", down)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )
    assert resp.status_code == 503, resp.text
    assert await _intents(sessions) == []
    assert fake_psp.checkout_sessions == []


async def test_a_failed_expire_is_counted_and_the_gate_remains_the_backstop(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    sandloom_core: Any,
) -> None:
    """Expiring is a COMPENSATION and can fail: the refusal still stands (no
    URL, intent revoked), the failure is counted for alerting
    (``billing_intent_expire_failed``) and not retried; the session stays
    payable at the PSP until it expires there, and the webhook gate refuses
    its completion."""
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    _demote_during(
        monkeypatch,
        fake_psp,
        "create_checkout_session",
        async_client,
        test_db,
        org_id,
        admin_id,
    )

    async def psp_down(session_id: str) -> None:
        raise ConnectionError("PSP unreachable")

    monkeypatch.setattr(fake_psp, "expire_checkout_session", psp_down)
    before = sandloom_core.get_metric("billing_intent_expire_failed") or 0

    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )

    assert resp.status_code == 403, resp.text
    [session] = fake_psp.checkout_sessions
    assert session["url"] not in resp.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert (sandloom_core.get_metric("billing_intent_expire_failed") or 0) == before + 1


# --------------------------------------------------------------------------- #
# The precise G1 order: tenancy's state AS OF the final re-read
# --------------------------------------------------------------------------- #


def _demote_now(
    client: AsyncClient, test_db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> Any:
    async def demote() -> None:
        # committed in tenancy, from another transaction
        await _demote(client, test_db, org_id, user_id)

    return demote


async def test_role_change_committed_before_the_final_reread_is_refused(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """(a) A demotion committed in tenancy immediately BEFORE the final
    re-read (after the PSP call, inside the issue transaction) is seen: the
    intent is revoked, the session expired, no URL."""
    from sandloom_billing.services import operation_intents

    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    demote = _demote_now(async_client, test_db, org_id, admin_id)
    original = operation_intents.reverify_authority

    async def demoted_then_reread(core: Any, ctx: Any) -> None:
        await demote()
        await original(core, ctx)

    monkeypatch.setattr(operation_intents, "reverify_authority", demoted_then_reread)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )

    assert resp.status_code == 403, resp.text
    [session] = fake_psp.checkout_sessions
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert fake_psp.expired_sessions == [session["id"]]


async def test_window_role_change_after_the_final_reread_is_not_seen(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """(b) THE ACKNOWLEDGED WINDOW. A demotion committed in tenancy AFTER the
    final re-read (version + session marks) but BEFORE billing's issue COMMIT
    is not seen: nothing ties tenancy's commits to billing's, so the intent is
    issued and the URL returned. Serializable ("checkout, then demotion"), not
    linearizable. This test documents the window; it is not a guarantee."""
    from sandloom_billing.services import operation_intents

    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    demote = _demote_now(async_client, test_db, org_id, admin_id)
    original = operation_intents.release_check

    async def last_reread_then_demote(account: Any, **kwargs: Any) -> None:
        await original(account, **kwargs)  # the final re-read: still admin
        # tenancy commits between it and billing's COMMIT (another actor:
        # its own context)
        await asyncio.create_task(demote(), context=contextvars.Context())

    monkeypatch.setattr(operation_intents, "release_check", last_reread_then_demote)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )

    assert resp.status_code == 200, resp.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED
    assert fake_psp.expired_sessions == []
    fresh_headers = await _fresh_login_headers(admin_id)
    role = await async_client.get(
        f"/api/v1/orgs/{org_id}/membership", headers=fresh_headers
    )
    assert role.status_code == 200, role.text
    assert role.json()["role"] == "member"


# --------------------------------------------------------------------------- #
# Ownership: application-level isolation (billing tables have no RLS)
# --------------------------------------------------------------------------- #


async def test_another_tenants_idempotency_key_is_never_reused(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Two users sending the SAME Idempotency-Key get their own intents and
    sessions: the retry lookup is scoped to the actor (and its customer)."""
    body = {"price_id": catalog["pro_month"].id}
    urls = []
    for _ in range(2):
        _, headers = make_user_headers()
        resp = await async_client.post(
            CHECKOUT, json=body, headers=_idem(headers, "shared-key")
        )
        assert resp.status_code == 200, resp.text
        urls.append(resp.json()["url"])
    intents = await _intents(sessions)
    assert len(intents) == 2 and len({i.customer_id for i in intents}) == 2
    assert len(set(urls)) == 2 and len(fake_psp.checkout_sessions) == 2


async def test_same_key_for_another_tenant_of_the_same_actor_is_refused(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One actor, one key, two tenants (personal, then an org): the second
    request must not get the first tenant's intent / session."""
    user_id, headers = make_user_headers()
    body = {"price_id": catalog["pro_month"].id}
    first = await async_client.post(
        CHECKOUT, json=body, headers=_idem(headers, "one-key")
    )
    assert first.status_code == 200, first.text
    owner = make_user_headers()
    org_id = await tenancy_api.create_org(async_client, owner[1], name="Two")
    await tenancy_api.add_member(
        async_client, org_id, owner[1], user_id, tenancy_api.ROLE_ADMIN
    )
    await test_db.commit()

    second = await async_client.post(
        CHECKOUT,
        json={**body, "org_id": str(org_id)},
        headers=_idem(headers, "one-key"),
    )
    assert second.status_code == 422, second.text
    assert first.json()["url"] not in second.text
    assert len(await _intents(sessions)) == 1


async def test_webhook_naming_another_customers_intent_is_not_applied(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """PSP metadata names an ISSUED intent of customer A on an event for
    customer B: not applied, audited, and A's intent is left untouched (a
    forged reference must not even flag another tenant's intent)."""
    body = {"price_id": catalog["pro_month"].id}
    _, headers_a = make_user_headers()
    _, headers_b = make_user_headers()
    for headers in (headers_a, headers_b):
        resp = await async_client.post(CHECKOUT, json=body, headers=_idem(headers))
        assert resp.status_code == 200, resp.text
    intent_a, intent_b = await _intents(sessions)
    async with sessions() as db:
        external_b = (
            await db.execute(
                select(BillingCustomer.stripe_customer_id).where(
                    BillingCustomer.id == intent_b.customer_id
                )
            )
        ).scalar_one()

    event, sig = _signed(_subscription_created(str(external_b), str(intent_a.id)))
    resp = await async_client.post(WEBHOOK, content=event, headers=sig)

    assert resp.json()["status"] == "failed", resp.text
    assert await _count_in(test_db, BillingSubscription) == 0
    assert await _audit_in(test_db, "billing.intent.unauthorized_completion") == 1
    untouched = await test_db.get(OperationIntent, intent_a.id, populate_existing=True)
    assert untouched is not None
    assert untouched.followup_at is None and untouched.status == INTENT_ISSUED


async def test_checkout_completion_must_name_the_intents_own_session(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A credit-pack completion whose metadata names an issued intent of the
    same customer but ANOTHER session is not applied."""
    _, headers = make_user_headers()
    body = {"price_id": catalog["pack_price"].id}
    resp = await async_client.post(CHECKOUT, json=body, headers=_idem(headers))
    assert resp.status_code == 200, resp.text
    [session] = fake_psp.checkout_sessions
    payload = {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": "cs_some_other_session",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": catalog["pack_price"].amount,
                "currency": "usd",
                "customer": session["customer"],
                "metadata": session["metadata"],
            }
        },
    }
    event, sig = _signed(payload)
    resp = await async_client.post(WEBHOOK, content=event, headers=sig)
    assert resp.json()["status"] == "failed", resp.text
    [intent] = await _intents(sessions)
    assert await BillingService.get_credit_balance(intent.customer_id, test_db) == 0


# --------------------------------------------------------------------------- #
# A retry of an ISSUED intent never calls the PSP again and never revokes it
# --------------------------------------------------------------------------- #


async def _forget_guard_records(sandloom_cache: Any) -> None:
    """The idempotency guard's record is gone (Redis flush / eviction / TTL)."""
    from sandloom.sdk.testing import flush_owned_redis

    client = await sandloom_cache.raw_client()
    await flush_owned_redis(client)


async def _issued_checkout(
    async_client: AsyncClient, catalog: dict[str, Any], headers: dict, key: str
) -> str:
    resp = await async_client.post(
        CHECKOUT,
        json={"price_id": catalog["pro_month"].id},
        headers=_idem(headers, key),
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["url"])


async def test_retry_of_an_issued_intent_returns_its_url_without_a_psp_call(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    sandloom_cache: Any,
) -> None:
    """Lost response / lost guard record, then a retry after the PSP's own
    idempotency window (the same key would now create a NEW session) and with
    the PSP rejecting: the issued intent answers with its stored URL after a
    fresh authority re-check, no PSP call, ``psp_session_id`` unchanged."""
    _, headers = make_user_headers()
    url = await _issued_checkout(async_client, catalog, headers, "issued-retry")
    [issued] = await _intents(sessions)
    await _forget_guard_records(sandloom_cache)
    fake_psp._checkout_by_idempotency_key.clear()  # Stripe forgot the key

    async def must_not_be_called(**_: Any) -> Any:
        raise AssertionError("an issued intent must not call the PSP again")

    monkeypatch.setattr(fake_psp, "create_checkout_session", must_not_be_called)
    resp = await async_client.post(
        CHECKOUT,
        json={"price_id": catalog["pro_month"].id},
        headers=_idem(headers, "issued-retry"),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == url
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED
    assert intent.psp_session_id == issued.psp_session_id
    assert fake_psp.expired_sessions == []


async def test_failed_recheck_on_retry_never_revokes_an_issued_intent(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    sandloom_cache: Any,
) -> None:
    """The retry's re-check fails (version moved: 409): the retry gets no
    URL, but the issued intent stays issued and its session is NOT expired —
    a legitimate payment on it must still be honoured by the webhook."""
    from sandloom_billing.services import operation_intents

    org_id, (_, admin_headers) = await _org(async_client, test_db)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers, "issued-409"),
    )
    assert resp.status_code == 200, resp.text
    await _forget_guard_records(sandloom_cache)

    async def moved(core: Any, ctx: Any) -> None:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail="membership changed")

    monkeypatch.setattr(operation_intents, "reverify_authority", moved)
    retry = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers, "issued-409"),
    )

    assert retry.status_code == 409, retry.text
    assert "https://" not in retry.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED
    assert fake_psp.expired_sessions == []
    assert len(fake_psp.checkout_sessions) == 1


async def test_retry_of_an_issued_portal_intent_is_refused_without_a_psp_call(
    async_client: AsyncClient,
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
    sandloom_cache: Any,
) -> None:
    """A portal URL is never stored, so it cannot be returned again: the
    retry of an issued portal intent is 409 (use a new Idempotency-Key)."""
    _, headers = make_user_headers()
    first = await async_client.post(PORTAL, json={}, headers=_idem(headers, "portal-k"))
    assert first.status_code == 200, first.text
    await _forget_guard_records(sandloom_cache)

    retry = await async_client.post(PORTAL, json={}, headers=_idem(headers, "portal-k"))

    assert retry.status_code == 409, retry.text
    assert first.json()["url"] not in retry.text
    assert len(fake_psp.portal_sessions) == 1
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED


async def test_demoted_manager_cannot_replay_a_portal_url_with_a_new_token(
    async_client: AsyncClient,
    test_db: AsyncSession,
    fake_psp: Any,
) -> None:
    """The F routes' idempotency guard dedupes in-flight requests only and
    never stores the response body (a portal URL is a bearer credential): a
    demoted manager who logs in again and replays the same key goes through
    the authorization path and gets 403, not the stored URL."""
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    first = await async_client.post(
        PORTAL, json={"org_id": str(org_id)}, headers=_idem(admin_headers, "replay-k")
    )
    assert first.status_code == 200, first.text
    await _demote(async_client, test_db, org_id, admin_id)
    fresh_headers = await _fresh_login_headers(admin_id)

    replay = await async_client.post(
        PORTAL, json={"org_id": str(org_id)}, headers=_idem(fresh_headers, "replay-k")
    )

    assert replay.status_code == 403, replay.text
    assert first.json()["url"] not in replay.text


# --------------------------------------------------------------------------- #
# Lifecycle invariants: issued is terminal for every HTTP-driven transition
# --------------------------------------------------------------------------- #


async def test_demoted_manager_retrying_an_issued_checkout_gets_no_url(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotency of the EFFECT is not idempotency of handing out a SECRET:
    an issued checkout's stored URL is reproduced only after a fresh authority
    check. Demoted + new token + same key -> 403, no URL, intent stays
    issued, no PSP call."""
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    body = {"org_id": str(org_id), "price_id": catalog["pro_month"].id}
    first = await async_client.post(
        CHECKOUT, json=body, headers=_idem(admin_headers, "secret-k")
    )
    assert first.status_code == 200, first.text
    await _demote(async_client, test_db, org_id, admin_id)
    fresh_headers = await _fresh_login_headers(admin_id)

    async def must_not_be_called(**_: Any) -> Any:
        raise AssertionError("no PSP call for an issued intent")

    monkeypatch.setattr(fake_psp, "create_checkout_session", must_not_be_called)
    retry = await async_client.post(
        CHECKOUT, json=body, headers=_idem(fresh_headers, "secret-k")
    )

    assert retry.status_code == 403, retry.text
    assert first.json()["url"] not in retry.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED
    assert fake_psp.expired_sessions == []


@pytest.mark.parametrize("target", ["revoked", "failed"])
async def test_closing_an_issued_intent_is_a_no_op(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    sessions: async_sessionmaker[AsyncSession],
    target: str,
) -> None:
    """issued -> revoked / failed is forbidden: the close path only moves a
    PENDING intent."""
    from sandloom_billing.services.operation_intents import (
        OperationIntentService,
        _Reserved,
    )

    _, headers = make_user_headers()
    await _issued_checkout(async_client, catalog, headers, f"close-{target}")
    [issued] = await _intents(sessions)
    reserved = _Reserved(
        intent_id=issued.id,
        customer_id=issued.customer_id,
        tenant_type=issued.tenant_type,
        tenant_id=issued.tenant_id,
        external_customer=None,
        plan=None,
    )

    closed = await OperationIntentService._close(
        reserved, target, reason="attempt", session_id="cs_other", actor_id=None
    )

    assert closed is False
    [after] = await _intents(sessions)
    assert after.status == INTENT_ISSUED
    assert after.psp_session_id == issued.psp_session_id
    assert await _audits(sessions, "billing.intent.revoked") == 0


async def test_issuing_an_issued_intent_again_is_refused(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """issued -> issued (a second finalize with another session) is
    forbidden: psp_session_id is never replaced, and the other session is not
    expired on the way out (its intent was not pending)."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from sandloom_billing.psp import PspSession
    from sandloom_billing.services.operation_intents import (
        OperationIntentService,
        _Reserved,
    )
    from sandloom_billing.services.tenant_resolver import TenantContext

    user_id, headers = make_user_headers()
    await _issued_checkout(async_client, catalog, headers, "issue-twice")
    [issued] = await _intents(sessions)
    reserved = _Reserved(
        intent_id=issued.id,
        customer_id=issued.customer_id,
        tenant_type=issued.tenant_type,
        tenant_id=issued.tenant_id,
        external_customer=None,
        plan=None,
    )
    ctx = TenantContext(tenant_type="user", tenant_id=user_id, user_id=user_id)
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})

    with pytest.raises(HTTPException) as exc:
        await OperationIntentService._finalize(
            None,
            request,
            ctx,
            reserved,
            fake_psp,
            session=PspSession(id="cs_other", url="x"),
        )

    assert exc.value.status_code == 409
    [after] = await _intents(sessions)
    assert after.status == INTENT_ISSUED
    assert after.psp_session_id == issued.psp_session_id
    assert fake_psp.expired_sessions == []
