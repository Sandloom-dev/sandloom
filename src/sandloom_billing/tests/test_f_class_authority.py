"""ADR-0003 gap G1: billing's class-F routes re-verify the caller's authority
AFTER the PSP call, before the session URL is handed out.

``checkout-session`` / ``customer-portal`` authorize on the owner's live
membership (``tenancy.membership.v1.get_member_state``: role AND the
per-(org, user) membership ``version``), call the PSP, and then re-read the
membership state — bypassing the request's contract-read memo. A demotion or
removal that committed while the PSP call was in flight is caught (403); any
other membership change (``version`` moved) answers 409 (retry). The PSP
session already exists then; it is NOT delivered: its operation intent is
revoked and a checkout session expired (``test_operation_intents.py``).

The concurrent change is COMMITTED inside the PSP call (as another request
would): these routes run their own short transactions, not the test session.
"""

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.tests import tenancy_api
from sandloom_billing.tests.conftest import make_user_headers

pytestmark = pytest.mark.asyncio


def _idem(headers: dict, key: str) -> dict:
    return {**headers, "Idempotency-Key": f"{key}-{uuid.uuid4().hex}"}


async def _org(
    client: AsyncClient, test_db: AsyncSession
) -> tuple[uuid.UUID, Any, Any]:
    """An org with an owner and an admin (who may manage billing)."""
    owner, admin = make_user_headers(), make_user_headers()
    org_id = await tenancy_api.create_org(client, owner[1], name="Acme")
    await tenancy_api.add_member(
        client, org_id, owner[1], admin[0], tenancy_api.ROLE_ADMIN
    )
    await test_db.commit()
    return org_id, owner, admin


def _during_psp(
    monkeypatch: pytest.MonkeyPatch,
    psp: Any,
    method: str,
    change: Callable[[], Awaitable[None]],
    test_db: AsyncSession,
) -> list[str]:
    """Run ``change`` (a committed membership change) INSIDE the PSP call;
    return the URLs the PSP produced."""
    original = getattr(psp, method)
    produced: list[str] = []

    async def _committed_change() -> None:
        await change()
        await test_db.commit()  # a COMMITTED change, from another transaction

    async def wrapped(**kwargs: Any) -> Any:
        # Another actor, concurrently: its own context, never the route's
        # (the route's PSP call holds no unit, spec §4.6).
        await asyncio.create_task(_committed_change(), context=contextvars.Context())
        result = await original(**kwargs)
        produced.append(str(getattr(result, "url", result)))
        return result

    monkeypatch.setattr(psp, method, wrapped)
    return produced


_ROUTES = [
    ("create_checkout_session", "/api/v1/billing/checkout-session", True),
    ("create_billing_portal_session", "/api/v1/billing/customer-portal", False),
]


@pytest.mark.parametrize(("method", "path", "with_price"), _ROUTES)
async def test_demotion_during_the_psp_call_withholds_the_session(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    with_price: bool,
) -> None:
    org_id, (_, owner_headers), (admin_id, admin_headers) = await _org(
        async_client, test_db
    )

    async def demote() -> None:
        await tenancy_api.change_role(
            async_client, org_id, owner_headers, admin_id, tenancy_api.ROLE_MEMBER
        )

    produced = _during_psp(monkeypatch, fake_psp, method, demote, test_db)
    body: dict[str, Any] = {"org_id": str(org_id)}
    if with_price:
        body["price_id"] = catalog["pro_month"].id
    resp = await async_client.post(path, json=body, headers=_idem(admin_headers, "g1"))

    assert resp.status_code == 403, resp.text
    assert produced, "the PSP call happened (the change raced it)"
    assert all(url not in resp.text for url in produced)


@pytest.mark.parametrize(("method", "path", "with_price"), _ROUTES)
async def test_removal_during_the_psp_call_withholds_the_session(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    with_price: bool,
) -> None:
    org_id, (_, owner_headers), (admin_id, admin_headers) = await _org(
        async_client, test_db
    )

    async def remove() -> None:
        await tenancy_api.remove_member(async_client, org_id, owner_headers, admin_id)

    produced = _during_psp(monkeypatch, fake_psp, method, remove, test_db)
    body: dict[str, Any] = {"org_id": str(org_id)}
    if with_price:
        body["price_id"] = catalog["pro_month"].id
    resp = await async_client.post(path, json=body, headers=_idem(admin_headers, "g1"))

    assert resp.status_code == 403, resp.text
    assert produced and all(url not in resp.text for url in produced)


async def test_other_membership_change_during_the_psp_call_is_409(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authority version moved but the role still allows the action (the
    admin became the owner): not a denial, but not the state that was
    authorized either — 409, retry."""
    org_id, (_, owner_headers), (admin_id, admin_headers) = await _org(
        async_client, test_db
    )

    async def transfer() -> None:
        await tenancy_api.transfer_ownership(
            async_client, org_id, owner_headers, admin_id
        )

    produced = _during_psp(
        monkeypatch, fake_psp, "create_billing_portal_session", transfer, test_db
    )
    resp = await async_client.post(
        "/api/v1/billing/customer-portal",
        json={"org_id": str(org_id)},
        headers=_idem(admin_headers, "g1-409"),
    )
    assert resp.status_code == 409, resp.text
    assert produced and all(url not in resp.text for url in produced)


async def test_implicit_single_org_is_reverified_too(
    async_client: AsyncClient,
    test_db: AsyncSession,
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``org_id``: the caller's PERSONAL account (spec §7.4), never the
    single organization; an organization is re-verified only when named."""
    org_id, _owner, (_admin_id, admin_headers) = await _org(async_client, test_db)
    resp = await async_client.post(
        "/api/v1/billing/customer-portal",
        json={},
        headers=_idem(admin_headers, "g1-implicit"),
    )
    assert resp.status_code == 200, resp.text
    assert str(org_id) not in resp.text


async def test_unchanged_membership_still_gets_the_session(
    async_client: AsyncClient, test_db: AsyncSession, catalog: dict[str, Any]
) -> None:
    org_id, _, (_, admin_headers) = await _org(async_client, test_db)
    resp = await async_client.post(
        "/api/v1/billing/checkout-session",
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers, "g1-ok"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"].startswith("https://")


async def test_personal_context_has_no_membership_to_reverify(
    async_client: AsyncClient, catalog: dict[str, Any]
) -> None:
    _, headers = make_user_headers()
    resp = await async_client.post(
        "/api/v1/billing/checkout-session",
        json={"price_id": catalog["pro_month"].id},
        headers=_idem(headers, "g1-personal"),
    )
    assert resp.status_code == 200, resp.text
