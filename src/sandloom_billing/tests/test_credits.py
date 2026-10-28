"""Credit ledger semantics: idempotency, non-negative balance, events."""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import committing_uow
from sandloom_billing.exceptions import (
    BillingError,
    CreditOperationConflictError,
    InsufficientCreditsError,
)
from sandloom_billing.models import CreditLedgerEntry
from sandloom_billing.services import BillingService


@pytest.mark.asyncio
class TestCredits:
    async def test_add_and_spend(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        balance = await BillingService.add_credits(
            customer, 100, "op-add-1", test_db, reason="seed"
        )
        assert balance == 100

        balance = await BillingService.spend_credits(
            customer, 30, "op-spend-1", test_db, reason="feature"
        )
        assert balance == 70
        assert await BillingService.get_credit_balance(customer.id, test_db) == 70

    async def test_spend_insufficient_402(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        customer_id = customer.id  # snapshot: service rollback expires the instance
        await BillingService.add_credits(customer, 10, "op-add", test_db)
        with pytest.raises(InsufficientCreditsError) as exc:
            await BillingService.spend_credits(customer, 11, "op-overdraft", test_db)
        assert exc.value.http_status == 402
        # Balance unchanged, failed ledger entry rolled back
        assert await BillingService.get_credit_balance(customer_id, test_db) == 10
        ledger = await test_db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.operation_key == "op-overdraft"
            )
        )
        assert ledger.scalar_one_or_none() is None

    async def test_spend_is_idempotent(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        customer_id = customer.id  # snapshot: replay path rolls back the session
        await BillingService.add_credits(customer, 100, "op-add", test_db)

        first = await BillingService.spend_credits(customer, 40, "op-same-key", test_db)
        replay = await BillingService.spend_credits(
            customer, 40, "op-same-key", test_db
        )
        assert first == 60
        assert replay == 60  # no double spend
        assert await BillingService.get_credit_balance(customer_id, test_db) == 60

    async def test_add_is_idempotent(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        first = await BillingService.add_credits(customer, 50, "op-grant", test_db)
        replay = await BillingService.add_credits(customer, 50, "op-grant", test_db)
        assert first == 50
        assert replay == 50

    async def test_customer_one_of_invariant(self, test_db: AsyncSession):
        with pytest.raises(ValueError):
            await BillingService.get_or_create_customer(test_db)
        with pytest.raises(ValueError):
            await BillingService.get_or_create_customer(
                test_db, user_id=uuid.uuid4(), organization_id=uuid.uuid4()
            )

    async def test_get_or_create_customer_is_stable(self, test_db: AsyncSession):
        user_id = uuid.uuid4()
        first = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        second = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        assert first.id == second.id


@pytest.mark.asyncio
class TestCreditIdempotencyHardening:
    async def test_operation_key_is_scoped_per_customer(self, test_db: AsyncSession):
        a = await BillingService.get_or_create_customer(test_db, user_id=uuid.uuid4())
        b = await BillingService.get_or_create_customer(test_db, user_id=uuid.uuid4())
        a_id, b_id = a.id, b.id
        assert await BillingService.add_credits(a, 10, "shared-key", test_db) == 10
        # Previously a global UNIQUE(operation_key) made B's grant a silent no-op.
        assert await BillingService.add_credits(b, 20, "shared-key", test_db) == 20
        assert await BillingService.get_credit_balance(a_id, test_db) == 10
        assert await BillingService.get_credit_balance(b_id, test_db) == 20

    async def test_same_key_different_amount_is_409(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        customer_id = customer.id
        await BillingService.add_credits(customer, 100, "grant-1", test_db)
        await BillingService.spend_credits(customer, 10, "spend-1", test_db)
        with pytest.raises(CreditOperationConflictError) as exc:
            await BillingService.spend_credits(customer, 20, "spend-1", test_db)
        assert exc.value.http_status == 409
        with pytest.raises(CreditOperationConflictError) as exc:
            # same key, opposite sign (a grant key reused for a spend)
            await BillingService.spend_credits(customer, 100, "grant-1", test_db)
        assert exc.value.http_status == 409
        assert await BillingService.get_credit_balance(customer_id, test_db) == 90

    async def test_replay_returns_original_result(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        await BillingService.add_credits(customer, 100, "g", test_db)
        first = await BillingService.spend_credits(customer, 30, "s1", test_db)
        await BillingService.spend_credits(customer, 5, "s2", test_db)
        replay = await BillingService.spend_credits(customer, 30, "s1", test_db)
        assert first == replay == 70  # not the current balance (65)
        assert await BillingService.add_credits(customer, 100, "g", test_db) == 100

    @pytest.mark.parametrize("amount", [0, -5, True, 10**13, 1.5])
    async def test_amount_bounds(self, test_db: AsyncSession, amount: object):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        with pytest.raises(ValueError):
            await BillingService.add_credits(customer, amount, "k", test_db)  # type: ignore[arg-type]

    async def test_a_zero_spend_is_recorded_and_idempotent(self, test_db: AsyncSession):
        """A free item (zero price, promo code): recorded as a delta-0 ledger
        row under its key, balance unchanged; the same key with another amount
        is 409, a negative amount is refused."""
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        customer_id = customer.id
        await BillingService.add_credits(customer, 7, "g", test_db)
        assert await BillingService.spend_credits(customer, 0, "free-1", test_db) == 7
        assert await BillingService.spend_credits(customer, 0, "free-1", test_db) == 7
        with pytest.raises(CreditOperationConflictError):
            await BillingService.spend_credits(customer, 5, "free-1", test_db)
        rows = await test_db.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.customer_id == customer_id)
            .order_by(CreditLedgerEntry.id)
        )
        assert [(row.delta, row.operation_key) for row in rows] == [
            (7, "g"),
            (0, "free-1"),
        ]
        assert await BillingService.get_credit_balance(customer_id, test_db) == 7
        with pytest.raises(ValueError):
            await BillingService.spend_credits(customer, -1, "neg", test_db)

    async def test_a_zero_spend_succeeds_on_a_negative_balance(
        self, test_db: AsyncSession
    ):
        """G-BILLING-22: a refund / dispute reversal may leave the balance
        negative; a free item is still recorded there (delta-0 ledger row
        under its key, the spent event, an idempotent replay), while a paid
        spend stays refused."""
        from sqlalchemy import text

        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        customer_id = customer.id
        await BillingService.add_credits(customer, 3, "grant", test_db)
        assert (
            await BillingService.reverse_credits(customer, 4, "reversal", test_db) == -1
        )
        assert await BillingService.spend_credits(customer, 0, "free", test_db) == -1
        assert await BillingService.spend_credits(customer, 0, "free", test_db) == -1
        with pytest.raises(CreditOperationConflictError):
            await BillingService.spend_credits(customer, 1, "free", test_db)
        with pytest.raises(InsufficientCreditsError):
            await BillingService.spend_credits(customer, 1, "paid", test_db)
        rows = await test_db.scalars(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.customer_id == customer_id)
            .order_by(CreditLedgerEntry.id)
        )
        assert [(r.delta, r.operation_key, r.balance_after) for r in rows] == [
            (3, "grant", 3),
            (-4, "reversal", -1),
            (0, "free", -1),
        ]
        assert await BillingService.get_credit_balance(customer_id, test_db) == -1
        spent = await test_db.execute(
            text(
                "SELECT payload FROM outbox_messages WHERE channel = "
                "'billing.credits.spent.v1' AND payload->>'customer_id' = :c"
            ),
            {"c": str(customer_id)},
        )
        assert [p["data"] for (p,) in spent] == [
            {"amount": 0, "balance": -1, "reason": ""}
        ]

    async def test_a_zero_spend_needs_no_prior_billing_customer(
        self, test_db: AsyncSession
    ):
        from sandloom_billing.credits import CreditsService

        org_id = uuid.uuid4()
        assert (
            await CreditsService.spend("organization", org_id, 0, "free", test_db) == 0
        )
        assert (
            await CreditsService.spend("organization", org_id, 0, "free", test_db) == 0
        )
        with pytest.raises(InsufficientCreditsError):
            await CreditsService.spend("organization", org_id, 1, "paid", test_db)

    async def test_large_grant_beyond_int32(self, test_db: AsyncSession):
        customer = await BillingService.get_or_create_customer(
            test_db, user_id=uuid.uuid4()
        )
        big = 5_000_000_000  # > 2**31: used to overflow INTEGER into a 500
        assert await BillingService.add_credits(customer, big, "big", test_db) == big


@pytest.mark.asyncio
class TestConcurrency:
    async def test_concurrent_spend_never_overdraws(self, test_engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        user_id = uuid.uuid4()
        async with committing_uow(maker) as s:
            customer = await BillingService.get_or_create_customer(s, user_id=user_id)
            await BillingService.add_credits(customer, 100, "seed", s)
            customer_id = customer.id

        async def _spend(i: int) -> int:
            async with committing_uow(maker) as s:
                c = await BillingService.get_or_create_customer(s, user_id=user_id)
                return await BillingService.spend_credits(c, 30, f"spend-{i}", s)

        results = await asyncio.gather(
            *(_spend(i) for i in range(6)), return_exceptions=True
        )
        ok = [r for r in results if isinstance(r, int)]
        denied = [r for r in results if isinstance(r, InsufficientCreditsError)]
        assert len(ok) == 3 and len(denied) == 3, results
        async with maker() as s:
            assert await BillingService.get_credit_balance(customer_id, s) == 10

    async def test_concurrent_same_key_spends_once(self, test_engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        user_id = uuid.uuid4()
        async with committing_uow(maker) as s:
            customer = await BillingService.get_or_create_customer(s, user_id=user_id)
            await BillingService.add_credits(customer, 100, "seed", s)
            customer_id = customer.id

        async def _spend() -> int:
            async with committing_uow(maker) as s:
                c = await BillingService.get_or_create_customer(s, user_id=user_id)
                return await BillingService.spend_credits(c, 40, "same-op", s)

        results = await asyncio.gather(*(_spend() for _ in range(4)))
        assert results == [60, 60, 60, 60]
        async with maker() as s:
            assert await BillingService.get_credit_balance(customer_id, s) == 60

    async def test_concurrent_customer_creation_converges(self, test_engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        org_id = uuid.uuid4()

        async def _create() -> uuid.UUID:
            async with maker() as s, s.begin():
                c = await BillingService.get_or_create_customer(
                    s, organization_id=org_id
                )
                return c.id

        ids = await asyncio.gather(*(_create() for _ in range(5)))
        assert len(set(ids)) == 1


@pytest.mark.asyncio
class TestCreditsContract:
    async def test_contract_registered(self, sandloom_core: Any):
        from sandloom_billing import CREDITS_SERVICE_V1
        from sandloom_billing.contracts import CreditsContract

        core = sandloom_core

        # The registered contract is the session-free CreditsContract, bound
        # to the contract runtime (billing's own UoW) by get_service.
        assert core.get_service(CREDITS_SERVICE_V1).contract_impl is CreditsContract

    async def test_contract_spend_requires_operation_key_and_no_session(
        self, test_db: AsyncSession
    ):
        from sandloom.sdk.errors import ContractIdempotencyKeyMissing
        from sandloom.sdk.testing import bind_contract_session
        from sandloom_billing.contracts import CreditsContract

        org_id = uuid.uuid4()
        customer = await BillingService.get_or_create_customer(
            test_db, organization_id=org_id
        )
        await BillingService.add_credits(customer, 50, "seed-c", test_db)
        with bind_contract_session(test_db):
            with pytest.raises(ContractIdempotencyKeyMissing):
                await CreditsContract.spend("organization", org_id, 5, "")
            balance = await CreditsContract.spend(
                "organization", org_id, 20, "op-c", reason="export"
            )
            assert balance == 30
            # Idempotent replay on the operation key.
            assert await CreditsContract.spend("organization", org_id, 20, "op-c") == 30
            assert await CreditsContract.get_balance("organization", org_id) == 30

    async def test_spend_via_contract(self, test_db: AsyncSession):
        from sandloom_billing.credits import CreditsService

        org_id = uuid.uuid4()
        with pytest.raises(InsufficientCreditsError) as exc:
            await CreditsService.spend("organization", org_id, 1, "op", test_db)
        assert isinstance(exc.value, BillingError)
        assert exc.value.http_status == 402

        customer = await BillingService.get_or_create_customer(
            test_db, organization_id=org_id
        )
        await BillingService.add_credits(customer, 50, "seed", test_db)
        balance = await CreditsService.spend(
            "organization", org_id, 20, "op", test_db, reason="export"
        )
        assert balance == 30
        replay = await CreditsService.spend("organization", org_id, 20, "op", test_db)
        assert replay == 30
        from sandloom_billing.entitlements import EntitlementsService

        assert (
            await EntitlementsService.get_credit_balance(
                "organization", org_id, test_db
            )
            == 30
        )
        with pytest.raises(ValueError):
            await CreditsService.spend("planet", org_id, 1, "x", test_db)


@pytest.mark.asyncio
class TestCreditsContractBoundary:
    async def test_contract_maps_domain_errors_to_http(self, test_db: AsyncSession):
        """Consumers see HTTPException (same status locally and over RPC)."""
        from sandloom.sdk.testing import bind_contract_session
        from sandloom_billing.contracts import CreditsContract

        org_id = uuid.uuid4()
        with bind_contract_session(test_db):
            with pytest.raises(HTTPException) as exc:
                await CreditsContract.spend("organization", org_id, 1, "nope")
        assert exc.value.status_code == 402
        assert exc.value.detail == "Insufficient credits"

    async def test_spend_must_match_the_bound_organization(self, test_db: AsyncSession):
        from sandloom.sdk.tenant import bind_tenant
        from sandloom.sdk.testing import bind_contract_session
        from sandloom_billing.contracts import CreditsContract

        org_id, other_org = uuid.uuid4(), uuid.uuid4()
        customer = await BillingService.get_or_create_customer(
            test_db, organization_id=org_id
        )
        await BillingService.add_credits(customer, 50, "seed-t", test_db)
        user_id = uuid.uuid4()
        personal = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await BillingService.add_credits(personal, 5, "seed-p", test_db)
        with bind_contract_session(test_db):
            with bind_tenant(other_org):
                with pytest.raises(HTTPException) as exc:
                    await CreditsContract.spend("organization", org_id, 5, "t-1")
                assert exc.value.status_code == 403
                # Personal wallets are not tenancy tenants: not checked.
                assert await CreditsContract.spend("user", user_id, 1, "p-1") == 4
            with bind_tenant(org_id):
                assert (
                    await CreditsContract.spend("organization", org_id, 5, "t-1") == 45
                )
            # No tenant bound (system / worker caller): allowed.
            assert await CreditsContract.spend("organization", org_id, 5, "t-2") == 40
