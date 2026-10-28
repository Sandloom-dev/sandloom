"""PostgreSQL round-trip budgets of representative HTTP requests.

Each request runs through the REAL request Unit of Work (no session override)
with the database behind a counting wire proxy. The request is issued once to
warm the connection (first-use type introspection, statement preparation), then
measured in steady state. Post-commit event dispatch runs OFF the request path
(background coalescer) and is drained before the count is taken only where a
test says so; the budgets below are REQUEST-PATH round trips.

A failing budget prints the exact round trips, so a regression is visible at a
glance. Budgets are ceilings: lowering one after an optimisation is welcome,
raising one needs a justification in the commit.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.testing import make_user_headers
from sandloom.core.unit_of_work.dispatch import drain_post_commit
from sandloom_billing.models import PRODUCT_TYPE_ONE_TIME, BillingPrice, BillingProduct
from sandloom_billing.psp import sign_stripe_payload
from sandloom_billing.services import BillingService

from .conftest import WEBHOOK_SECRET
from .pgcount import PgCountingProxy

pytestmark = pytest.mark.asyncio


async def _measure(
    proxy: PgCountingProxy, send: Any, *, warmups: int = 1
) -> tuple[Any, int, str]:
    """Run ``send()`` ``warmups`` times, then once measured (request path only)."""
    for _ in range(warmups):
        resp = await send()
        assert resp.status_code < 300, resp.text
        await drain_post_commit()
    proxy.reset()
    resp = await send()
    assert resp.status_code < 300, resp.text
    trace = proxy.reset()
    await drain_post_commit()
    proxy.reset()
    return resp, len(trace), proxy.report(trace)


def _sessions(db_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def test_auth_me_budget(
    counted_client: Any, pg_proxy: PgCountingProxy, db_engine: Any
) -> None:
    from sandloom_identity.models.user import User

    user_id = uuid.uuid4()
    async with _sessions(db_engine)() as s:
        s.add(
            User(
                id=user_id,
                email=f"perf-{user_id.hex[:8]}@example.com",
                full_name="Perf Budget",
                is_active=True,
                is_verified=True,
            )
        )
        await s.commit()
    _, headers = make_user_headers(user_id, tenant=False)

    _, rts, report = await _measure(
        pg_proxy, lambda: counted_client.get("/auth/me", headers=headers)
    )
    print(f"\nGET /auth/me: {rts} round trips\n{report}")
    assert rts <= BUDGETS["auth_me"], report


async def test_list_my_orgs_budget(
    counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    _, headers = make_user_headers(tenant=False)
    _, rts, report = await _measure(
        pg_proxy, lambda: counted_client.get("/api/v1/orgs", headers=headers)
    )
    print(f"\nGET /api/v1/orgs: {rts} round trips\n{report}")
    assert rts <= BUDGETS["list_orgs"], report


async def test_create_org_budget(
    counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    _, headers = make_user_headers(tenant=False)

    def _create() -> Any:
        slug = f"perf-{uuid.uuid4().hex[:12]}"
        return counted_client.post(
            "/api/v1/orgs", json={"name": "Perf Org", "slug": slug}, headers=headers
        )

    _, rts, report = await _measure(pg_proxy, _create)
    print(f"\nPOST /api/v1/orgs: {rts} round trips\n{report}")
    assert rts <= BUDGETS["create_org"], report


async def test_list_members_budget(
    counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    _, headers = make_user_headers(tenant=False)
    created = await counted_client.post(
        "/api/v1/orgs",
        json={"name": "Perf Org", "slug": f"perf-{uuid.uuid4().hex[:12]}"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["id"]
    await drain_post_commit()
    _, rts, report = await _measure(
        pg_proxy,
        lambda: counted_client.get(f"/api/v1/orgs/{org_id}/members", headers=headers),
    )
    print(f"\nGET /api/v1/orgs/{{id}}/members: {rts} round trips\n{report}")
    assert rts <= BUDGETS["list_members"], report


async def test_role_change_budget(
    counted_client: Any, pg_proxy: PgCountingProxy, db_engine: Any
) -> None:
    from sandloom_tenancy.models import OrganizationMember

    _owner, headers = make_user_headers(tenant=False)
    created = await counted_client.post(
        "/api/v1/orgs",
        json={"name": "Perf Org", "slug": f"perf-{uuid.uuid4().hex[:12]}"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["id"]
    await drain_post_commit()
    member = uuid.uuid4()
    async with _sessions(db_engine)() as s:
        s.add(
            OrganizationMember(
                organization_id=uuid.UUID(org_id), user_id=member, role="member"
            )
        )
        await s.commit()
    roles = iter(["admin", "member"] * 4)

    def _change() -> Any:
        return counted_client.patch(
            f"/api/v1/orgs/{org_id}/members/{member}",
            json={"role": next(roles)},
            headers=headers,
        )

    resp, rts, report = await _measure(pg_proxy, _change)
    assert resp.json()["role"] in ("admin", "member")
    print(f"\nPATCH member role: {rts} round trips\n{report}")
    assert rts <= BUDGETS["role_change"], report


async def test_credits_read_budget(
    counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    _, headers = make_user_headers(tenant=False)
    _, rts, report = await _measure(
        pg_proxy,
        lambda: counted_client.get("/api/v1/billing/credits", headers=headers),
    )
    print(f"\nGET /api/v1/billing/credits: {rts} round trips\n{report}")
    assert rts <= BUDGETS["credits"], report


async def test_subscription_read_budget(
    counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    _, headers = make_user_headers(tenant=False)
    _, rts, report = await _measure(
        pg_proxy,
        lambda: counted_client.get("/api/v1/billing/subscription", headers=headers),
    )
    print(f"\nGET /api/v1/billing/subscription: {rts} round trips\n{report}")
    assert rts <= BUDGETS["subscription"], report


async def test_checkout_webhook_budget(
    counted_client: Any, pg_proxy: PgCountingProxy, db_engine: Any
) -> None:
    async with _sessions(db_engine)() as s:
        product = BillingProduct(
            name="100 credits", type=PRODUCT_TYPE_ONE_TIME, stripe_product_id="prod_pb"
        )
        s.add(product)
        await s.flush()
        price = BillingPrice(
            product_id=product.id,
            amount=1900,
            currency="USD",
            recurring_interval=None,
            stripe_price_id="price_pb_100",
            credits=100,
        )
        s.add(price)
        customer = await BillingService.get_or_create_customer(s, user_id=uuid.uuid4())
        customer.stripe_customer_id = "cus_perf_budget"
        await s.commit()
        price_id, customer_id = price.id, customer.id

    def _webhook() -> Any:
        event = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": "checkout.session.completed",
            "created": int(time.time()),
            "data": {
                "object": {
                    "id": f"cs_{uuid.uuid4().hex}",
                    "mode": "payment",
                    "payment_status": "paid",
                    "payment_intent": f"pi_{uuid.uuid4().hex}",
                    "amount_total": 1900,
                    "currency": "usd",
                    "customer": "cus_perf_budget",
                    "metadata": {
                        "customer_id": str(customer_id),
                        "price_id": str(price_id),
                    },
                }
            },
        }
        body = json.dumps(event).encode()
        return counted_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=body,
            headers={
                "Stripe-Signature": sign_stripe_payload(body, WEBHOOK_SECRET),
                "Content-Type": "application/json",
            },
        )

    resp, rts, report = await _measure(pg_proxy, _webhook)
    assert resp.json()["status"] == "processed", resp.text
    print(f"\nPOST stripe webhook (credit pack): {rts} round trips\n{report}")
    # The restored request context adds the mandatory money-effect audit.
    assert report.count("B:INSERT INTO audit_log ") == 1, report
    assert rts <= BUDGETS["webhook"], report


#: Request-path PostgreSQL round-trip ceilings (steady state, warm connection).
BUDGETS: dict[str, int] = {
    "auth_me": 3,  # BEGIN, user, COMMIT (was 4 with the unused roles selectin)
    "list_orgs": 3,  # BEGIN, orgs, COMMIT (was 6)
    # dispatch + mark run off the request path (was 18); +2 since the owner's
    # tenancy.member_added.v1 (membership version upsert + its outbox row)
    "create_org": 11,
    "list_members": 4,  # BEGIN, role gate, members, COMMIT (was 7)
    # BEGIN, role gate, lock target + actor, UPDATE, member version upsert,
    # audit + both outbox rows (one fused statement), COMMIT; was 10 with a
    # refresh SELECT after the UPDATE and one INSERT per audit / outbox table
    "role_change": 8,
    "credits": 6,  # tenancy contract UoW (3) + billing UoW (3) (was 7)
    "subscription": 6,  # tenancy contract UoW (3) + billing UoW (3) (was 7)
    # journal + apply + ledger (was 23; 12 before ADR-0003 item 12); +1 the
    # credit-grant link row a refund / dispute needs, +1 the payment-state
    # upsert (locks the row, out-of-order refunds; Stripe sends
    # payment_intent on every paid session) — same transaction
    # +1 mandatory sensitive-write audit at dependency commit (C022 remediation).
    "webhook": 15,
}
