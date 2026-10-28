"""Owner billing admission, safe projections and DB-atomic operator commands."""

import uuid

import pytest
from sqlalchemy import func, select

from sandloom.sdk.testing import as_user
from sandloom_billing.models import BillingCustomer, CreditBalance, CreditLedgerEntry
from sandloom_billing.models.intent import OperationIntent

PREFIX = "/api/v1/platform/billing"


def operator():
    return as_user(roles=["admin"], claims_sources=["identity"], tenant=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "customers",
        "subscriptions",
        "products",
        "prices",
        "balances",
        "ledger",
        "grants",
        "webhooks",
        "intents",
        "payment-states",
        "provider",
    ],
)
async def test_all_operator_reads_deny_tenant_accounts(async_client, user_a, path):
    assert (await async_client.get(f"{PREFIX}/{path}")).status_code == 401
    assert (
        await async_client.get(f"{PREFIX}/{path}", headers=user_a[1])
    ).status_code == 403
    _, forged = as_user(roles=["admin"], claims_sources=[], tenant=False)
    assert (
        await async_client.get(f"{PREFIX}/{path}", headers=forged)
    ).status_code == 403
    response = await async_client.get(f"{PREFIX}/{path}", headers=operator()[1])
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_credit_adjustment_replay_binds_payload_and_returns_original_result(
    async_client, test_db
):
    actor, headers = operator()
    customer = BillingCustomer(user_id=uuid.uuid4())
    test_db.add(customer)
    await test_db.flush()
    cid = customer.id
    path = f"{PREFIX}/customers/{cid}/adjust-credits"
    command = {
        "direction": "grant",
        "amount": 100,
        "reason": "Support ticket ABC-123",
        "operation_key": str(uuid.uuid4()),
    }
    first = await async_client.post(path, headers=headers, json=command)
    assert first.status_code == 200, first.text
    assert first.json()["balance_after"] == 100
    assert not first.json()["replayed"]
    # Another legitimate command changes the balance; replay must return 100.
    second = {**command, "amount": 30, "operation_key": str(uuid.uuid4())}
    assert (
        await async_client.post(path, headers=headers, json=second)
    ).status_code == 200
    retry = await async_client.post(path, headers=headers, json=command)
    assert retry.status_code == 200, retry.text
    assert retry.json()["replayed"] and retry.json()["balance_after"] == 100
    for change in (
        {"amount": 101},
        {"reason": "Different support reason"},
        {"direction": "deduct"},
    ):
        conflict = await async_client.post(
            path, headers=headers, json={**command, **change}
        )
        assert conflict.status_code == 409, conflict.text
    assert (
        await test_db.scalar(
            select(CreditBalance.amount).where(CreditBalance.customer_id == cid)
        )
        == 130
    )
    assert (
        await test_db.scalar(
            select(func.count())
            .select_from(CreditLedgerEntry)
            .where(CreditLedgerEntry.customer_id == cid)
        )
        == 2
    )
    audit = await async_client.get(
        "/api/v1/platform/billing/audit",
        headers=headers,
        params={"action": "billing.platform.credit_adjusted", "actor_id": str(actor)},
    )
    assert audit.status_code == 200, audit.text
    assert isinstance(audit.json(), list)


@pytest.mark.asyncio
async def test_credit_command_validation_and_authorization(
    async_client, test_db, user_a
):
    customer = BillingCustomer(user_id=uuid.uuid4())
    test_db.add(customer)
    await test_db.flush()
    path = f"{PREFIX}/customers/{customer.id}/adjust-credits"
    command = {
        "direction": "grant",
        "amount": 100,
        "reason": "Support ticket ABC-123",
        "operation_key": str(uuid.uuid4()),
    }
    assert (
        await async_client.post(path, headers=user_a[1], json=command)
    ).status_code == 403
    for change in (
        {"amount": 0},
        {"amount": -1},
        {"amount": True},
        {"amount": 1.5},
        {"reason": "        "},
        {"operation_key": "bad"},
        {"unexpected": True},
    ):
        result = await async_client.post(
            path, headers=operator()[1], json={**command, **change}
        )
        assert result.status_code == 422, result.text
    deduct = await async_client.post(
        path, headers=operator()[1], json={**command, "direction": "deduct"}
    )
    assert deduct.status_code == 402, deduct.text
    assert (
        await test_db.scalar(select(func.count()).select_from(CreditLedgerEntry)) == 0
    )


@pytest.mark.asyncio
async def test_intent_view_omits_bearer_url_and_internal_keys(async_client, test_db):
    user_id = uuid.uuid4()
    customer = BillingCustomer(user_id=user_id)
    test_db.add(customer)
    await test_db.flush()
    intent = OperationIntent(
        customer_id=customer.id,
        tenant_type="user",
        tenant_id=user_id,
        actor_id=user_id,
        operation="checkout",
        status="pending",
        params_digest="private-digest",
        psp_idempotency_key="private-psp-key",
        psp_session_url="https://secret-checkout.invalid/bearer",
        reconcile_error="private-provider-error",
    )
    test_db.add(intent)
    await test_db.flush()
    result = await async_client.get(
        f"{PREFIX}/intents/{intent.id}", headers=operator()[1]
    )
    assert result.status_code == 200, result.text
    assert "private" not in result.text and "secret-checkout" not in result.text
    assert (
        not {
            "psp_session_url",
            "psp_idempotency_key",
            "params_digest",
            "request_key",
            "reconcile_error",
        }
        & result.json().keys()
    )


@pytest.mark.asyncio
async def test_catalog_edit_audits_reason_and_cannot_rewrite_price(
    async_client, catalog
):
    headers = operator()[1]
    products = await async_client.get(f"{PREFIX}/products", headers=headers)
    pid = products.json()[0]["id"]
    changed = await async_client.patch(
        f"{PREFIX}/products/{pid}",
        headers=headers,
        json={
            "name": "Archived offer",
            "is_active": False,
            "reason": "Retire old offer ABC-234",
        },
    )
    assert changed.status_code == 200, changed.text
    assert not changed.json()["is_active"]
    price = (await async_client.get(f"{PREFIX}/prices", headers=headers)).json()[0]
    assert (
        await async_client.patch(
            f"{PREFIX}/prices/{price['id']}", headers=headers, json={"amount": 1}
        )
    ).status_code == 405


@pytest.mark.asyncio
@pytest.mark.sandloom_allow_server_errors  # asserts the masked 500 itself
async def test_committed_adjustment_is_concurrent_safe_and_audit_failure_rolls_back(
    uow_client, test_db, sandloom_cache, monkeypatch
):
    import asyncio

    from sandloom.sdk.errors import AuditUnavailableError
    from sandloom_billing.services import platform_service

    customer = BillingCustomer(user_id=uuid.uuid4())
    test_db.add(customer)
    await test_db.commit()
    cid = customer.id
    actor, headers = operator()
    command = {
        "direction": "grant",
        "amount": 100,
        "reason": "Concurrent support operation",
        "operation_key": str(uuid.uuid4()),
    }
    path = f"{PREFIX}/customers/{cid}/adjust-credits"
    responses = await asyncio.gather(
        *(uow_client.post(path, headers=headers, json=command) for _ in range(2))
    )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert sorted(r.json()["replayed"] for r in responses) == [False, True]
    audit = await uow_client.get(
        "/api/v1/platform/billing/audit",
        headers=headers,
        params={"actor_id": str(actor), "action": "billing.platform.credit_adjusted"},
    )
    assert audit.status_code == 200 and len(audit.json()) == 1, audit.text
    assert {"actor_id", "action", "resource_id", "request_id"} <= audit.json()[0].keys()
    assert "data" not in audit.json()[0]
    assert audit.json()[0]["occurred_at"].endswith(("Z", "+00:00"))

    def refuse_audit(*args, **kwargs):
        raise AuditUnavailableError("Injected audit failure")

    monkeypatch.setattr(platform_service, "record_audit", refuse_audit)
    failed = await uow_client.post(
        path, headers=headers, json={**command, "operation_key": str(uuid.uuid4())}
    )
    assert failed.status_code >= 500, failed.text
    assert (
        await test_db.scalar(
            select(CreditBalance.amount).where(CreditBalance.customer_id == cid)
        )
        == 100
    )
    assert (
        await test_db.scalar(
            select(func.count())
            .select_from(CreditLedgerEntry)
            .where(CreditLedgerEntry.customer_id == cid)
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["identity", "tenancy", "billing"])
@pytest.mark.parametrize("view", ["audit", "services"])
async def test_operations_views_are_strict_operator_only(
    async_client, user_a, source, view
):
    path = f"/api/v1/platform/{source}/{view}"
    assert (await async_client.get(path)).status_code == 401
    assert (await async_client.get(path, headers=user_a[1])).status_code == 403
    _, invalid = as_user(roles=["admin"], claims_sources=[])
    assert (await async_client.get(path, headers=invalid)).status_code == 403
    result = await async_client.get(path, headers=operator()[1])
    assert result.status_code == 200, result.text
    assert not any(
        value in result.text
        for value in (
            "postgresql://",
            "redis://",
            "internal_token",
            "password",
            "base_url",
        )
    )


@pytest.mark.asyncio
async def test_operator_lock_does_not_block_concurrent_ordinary_credit_spend(
    uow_client, test_db, sandloom_cache, monkeypatch
):
    import asyncio

    from sandloom.core.di import db_session
    from sandloom_billing.services.billing_service import BillingService

    customer = BillingCustomer(user_id=uuid.uuid4())
    test_db.add(customer)
    await test_db.flush()
    cid = customer.id
    test_db.add(CreditBalance(customer_id=cid, amount=100))
    test_db.add(
        CreditLedgerEntry(
            customer_id=cid,
            delta=100,
            operation_key="seed",
            reason="Fixture",
            balance_after=100,
        )
    )
    await test_db.commit()
    locked, release = asyncio.Event(), asyncio.Event()
    grant = BillingService.add_credits

    async def pause_grant(customer, amount, operation_key, db, reason="", **kwargs):
        locked.set()  # Operator service already owns the customer lock.
        await release.wait()
        return await grant(customer, amount, operation_key, db, reason, **kwargs)

    monkeypatch.setattr(BillingService, "add_credits", staticmethod(pause_grant))
    command = {
        "direction": "grant",
        "amount": 20,
        "reason": "Concurrent normal spending",
        "operation_key": str(uuid.uuid4()),
    }
    request = asyncio.create_task(
        uow_client.post(
            f"{PREFIX}/customers/{cid}/adjust-credits",
            headers=operator()[1],
            json=command,
        )
    )
    try:
        await asyncio.wait_for(locked.wait(), 3)

        async def ordinary_spend():
            async with db_session() as db:
                current = await db.get(BillingCustomer, cid)
                return await BillingService.spend_credits(
                    current, 10, "ordinary-spend", db, reason="Usage"
                )

        # A full customer UPDATE lock makes the ledger FK check wait here.
        assert await asyncio.wait_for(ordinary_spend(), 2) == 90
    finally:
        release.set()
        response = await request
    assert response.status_code == 200, response.text
    assert response.json()["balance_after"] == 110
