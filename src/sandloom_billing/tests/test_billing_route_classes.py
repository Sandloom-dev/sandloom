"""Billing routes under model E (ADR-0009): every authenticated billing route
is strict.

There are no route classes to pick and no staleness budget to declare: every
authenticated request reads the subject-state record live. These tests pin
the billing surface and that a revoked token / blocked user is rejected on
the very next request on every authenticated billing route.

Real Postgres (``db``) and Redis (``sandloom_cache``).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient

from sandloom.sdk.testing import block_mark_key, revocation_mark_key, route_policy

PREFIX = "/api/v1/billing"

READ_ROUTES = {
    ("GET", f"{PREFIX}/plans"),  # public catalog (no subject)
    ("GET", f"{PREFIX}/subscription"),
    ("GET", f"{PREFIX}/credits"),
}
FINANCIAL_ROUTES = {
    ("POST", f"{PREFIX}/checkout-session"),
    ("POST", f"{PREFIX}/customer-portal"),
}
#: PSP-signed, no subject.
WEBHOOK_ROUTES = {("POST", f"{PREFIX}/webhooks/stripe")}


def _billing_routes(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    out: dict[tuple[str, str], APIRoute] = {}
    for route in app.router.routes:
        if isinstance(route, APIRoute) and route.path.startswith(PREFIX):
            for method in route.methods:
                out[(method, route.path)] = route
    return out


def test_every_billing_route_is_classified(sandloom_app: FastAPI) -> None:
    """The billing surface is exactly these routes (a new route is approved in
    billing's routes.snapshot.json)."""
    assert set(_billing_routes(sandloom_app)) == (
        READ_ROUTES | FINANCIAL_ROUTES | WEBHOOK_ROUTES
    )


def test_read_routes_are_strict_today_and_declare_no_budget(
    sandloom_app: FastAPI,
) -> None:
    routes = _billing_routes(sandloom_app)
    for key in READ_ROUTES:
        policy = route_policy(routes[key])
        assert policy.staleness_budget == 0.0, key
        assert not policy.declares_budget, key


def test_financial_routes_are_always_strict(sandloom_app: FastAPI) -> None:
    routes = _billing_routes(sandloom_app)
    for key in FINANCIAL_ROUTES:
        policy = route_policy(routes[key])
        assert policy.staleness_budget == 0.0, key
        assert policy.always_strict, key


def _requests(price_id: int) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("GET", "/plans", None),
        ("GET", "/subscription", None),
        ("GET", "/credits", None),
        ("POST", "/checkout-session", {"price_id": price_id}),
        ("POST", "/customer-portal", {}),
    ]


async def _call(
    http: AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
) -> tuple[int, Any]:
    h = dict(headers)
    if method == "POST":
        h["Idempotency-Key"] = uuid.uuid4().hex
    resp = await http.request(method, PREFIX + path, json=body, headers=h)
    return resp.status_code, resp.json()


@pytest.mark.asyncio
async def test_revoked_token_rejected_on_every_route_like_before(
    async_client: AsyncClient, sandloom_cache: Any, user_a: Any, catalog: Any
) -> None:
    user_id, headers = user_a
    await sandloom_cache.set(
        revocation_mark_key(user_id),
        f"v1:after:{int(time.time()) + 1}",
        ttl_seconds=120,
    )
    for method, path, body in _requests(1):
        status, payload = await _call(async_client, method, path, body, headers)
        if path == "/plans":  # public: no subject, unaffected
            assert status == 200
            continue
        assert (status, payload["detail"]) == (401, "Token has been revoked"), path


@pytest.mark.asyncio
async def test_blocked_user_rejected_on_every_route_like_before(
    async_client: AsyncClient,
    sandloom_cache: Any,
    user_a: Any,
    catalog: Any,
    test_db: Any,
) -> None:
    user_id, headers = user_a
    for method, path, body in _requests(catalog["pro_month"].id):
        assert (await _call(async_client, method, path, body, headers))[0] == 200, path
        await test_db.commit()
    await sandloom_cache.set(block_mark_key(user_id), f"v1:blocked:{int(time.time())}")
    for method, path, body in _requests(catalog["pro_month"].id):
        status, payload = await _call(async_client, method, path, body, headers)
        if path == "/plans":
            assert status == 200
            continue
        assert (status, payload["detail"]) == (401, "Account blocked"), path
