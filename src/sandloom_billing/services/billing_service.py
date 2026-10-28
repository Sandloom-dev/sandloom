"""Billing service (BILLING_SPEC §5, §9).

Implements IBillingService: customer lifecycle, subscription status, credits
(idempotent ledger + materialized balance), checkout/portal sessions.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import BindParameter, ColumnElement, Select, bindparam, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from sandloom.sdk.data import Reads
from sandloom.sdk.infra import settings
from sandloom_billing import events
from sandloom_billing.exceptions import (
    CreditOperationConflictError,
    InsufficientCreditsError,
)
from sandloom_billing.models import (
    PRODUCT_TYPE_SUBSCRIPTION,
    STATUS_ACTIVE,
    STATUS_PAST_DUE,
    STATUS_TRIALING,
    BillingCustomer,
    BillingPrice,
    BillingProduct,
    BillingSubscription,
    CreditBalance,
    CreditLedgerEntry,
)
from sandloom_billing.models.subscription import (
    SubscriptionAccessView,
    subscription_grants_access,
)
from sandloom_billing.psp import PspSession, StripeClientPort
from sandloom_billing.schemas import PlanInfo, PlansOut, ProductOut, SubscriptionOut
from sandloom_billing.schemas.reads import SubscriptionRow
from sandloom_billing.services.operations import (
    balance_of,
    plan_of_price,
    subscriptions_of,
)

#: A subscription the access predicate reads (ORM row or declared-read DTO).
_Sub = TypeVar("_Sub", bound=SubscriptionAccessView)
_SubsWithPlans = Select[UUID, BillingSubscription, str]

#: Upper bound for ONE credit operation. Balances are BIGINT; bounding each
#: operation keeps a bogus amount a clean validation error instead of an
#: overflow 500 (and caps the blast radius of a buggy caller).
MAX_CREDIT_OPERATION = 1_000_000_000_000

#: Claim status emitted when the latest subscription carries a paying status
#: but no longer grants access (period elapsed / missing): never reads as
#: "active" to a claim-based gate.
CLAIM_STATUS_EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CheckoutPlan:
    """What a checkout buys: our price id, the PSP price id and the mode."""

    price_id: int
    external_price_id: str
    mode: str  # "subscription" | "payment"


class _SpendNotApplied(Exception):
    """Rolls back the spend savepoint (overdraft or replayed key)."""


class BillingService:
    """Core billing operations. All methods receive an explicit session."""

    # --- Customers ----------------------------------------------------------

    @classmethod
    async def _select_customer(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID | None,
        organization_id: UUID | None,
    ) -> BillingCustomer | None:
        query = select(BillingCustomer)
        if user_id is not None:
            query = query.where(BillingCustomer.user_id == user_id)
        else:
            query = query.where(BillingCustomer.organization_id == organization_id)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @classmethod
    async def get_or_create_customer(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
    ) -> BillingCustomer:
        """Return the tenant's customer, creating it race-free.

        ``INSERT … ON CONFLICT DO NOTHING`` then re-SELECT: two concurrent first
        requests for one tenant converge on the same row instead of one of them
        failing with a unique violation (500).
        """
        if (user_id is None) == (organization_id is None):
            raise ValueError("Exactly one of user_id / organization_id is required")

        customer = await cls._select_customer(
            db, user_id=user_id, organization_id=organization_id
        )
        if customer is not None:
            return customer

        inserted = await db.execute(
            pg_insert(BillingCustomer)
            .values(id=uuid.uuid4(), user_id=user_id, organization_id=organization_id)
            # create-or-get of the customer row, not a dedupe
            .on_conflict_do_nothing()
            .returning(BillingCustomer.id)
        )
        new_id = inserted.scalar_one_or_none()
        if new_id is not None:
            await db.execute(
                pg_insert(CreditBalance)
                .values(customer_id=new_id, amount=0)
                # create-or-get of the balance row, not a dedupe
                .on_conflict_do_nothing(index_elements=[CreditBalance.customer_id])
            )
        customer = await cls._select_customer(
            db, user_id=user_id, organization_id=organization_id
        )
        if customer is None:  # pragma: no cover - conflict row must be visible
            raise RuntimeError("billing customer vanished after upsert")
        return customer

    @classmethod
    async def ensure_psp_customer(
        cls, customer: BillingCustomer, psp: StripeClientPort, db: AsyncSession
    ) -> str:
        """Lazily create the PSP-side customer; persist its external id.

        The PSP call runs WITHOUT a row lock: a deterministic, tenant-derived
        idempotency key makes concurrent first calls return the same PSP
        customer, and a conditional ``UPDATE … WHERE stripe_customer_id IS
        NULL`` lets the first writer win. Holding ``FOR UPDATE`` across an HTTP
        call with retries/backoff would block every other request for this
        customer for seconds.
        """
        if customer.stripe_customer_id:
            return customer.stripe_customer_id

        customer_id = customer.id
        tenant_type = customer.tenant_type
        tenant_id = customer.tenant_id
        external_id = await cls.create_psp_customer(psp, tenant_type, tenant_id)
        stored = await cls.link_psp_customer(customer_id, external_id, db)
        set_committed_value(customer, "stripe_customer_id", stored)
        return stored

    @staticmethod
    async def create_psp_customer(
        psp: StripeClientPort, tenant_type: str, tenant_id: UUID
    ) -> str:
        """Create (idempotently, keyed by the tenant) the PSP-side customer."""
        return await psp.create_customer(
            metadata={"tenant_type": tenant_type, "tenant_id": str(tenant_id)},
            idempotency_key=f"sandloom:billing_customer:{tenant_type}:{tenant_id}",
        )

    @classmethod
    async def link_psp_customer(
        cls, customer_id: UUID, external_id: str, db: AsyncSession
    ) -> str:
        """Store ``external_id`` unless a concurrent request linked one first;
        return the stored id (first writer wins)."""
        result = await db.execute(
            update(BillingCustomer)
            .where(
                BillingCustomer.id == customer_id,
                BillingCustomer.stripe_customer_id.is_(None),
            )
            .values(stripe_customer_id=external_id)
            .returning(BillingCustomer.stripe_customer_id)
            .execution_options(synchronize_session=False)
        )
        stored = result.scalar_one_or_none()
        if stored is None:
            # A concurrent request linked the customer first: use its value.
            existing = await db.execute(
                select(BillingCustomer.stripe_customer_id).where(
                    BillingCustomer.id == customer_id
                )
            )
            stored = existing.scalar_one()
        assert stored is not None  # noqa: S101 - type narrowing of an invariant
        return str(stored)

    # --- Catalog ------------------------------------------------------------

    @classmethod
    async def list_plans(cls, db: AsyncSession) -> PlansOut:
        # The catalog is the one reader of the prices collection: load it here
        # explicitly (the relationship itself no longer eager-loads, so every
        # other product read — webhooks, plan info — skips that SELECT).
        result = await db.execute(
            select(BillingProduct)
            .options(selectinload(BillingProduct.prices))
            .where(BillingProduct.is_active.is_(True))
            .order_by(BillingProduct.id)
        )
        products = list(result.scalars().unique().all())
        return PlansOut(products=[ProductOut.model_validate(p) for p in products])

    # --- Subscriptions ------------------------------------------------------

    @classmethod
    async def _subscriptions(
        cls, customer_id: UUID, db: AsyncSession
    ) -> list[BillingSubscription]:
        result = await db.execute(
            select(BillingSubscription)
            .where(BillingSubscription.customer_id == customer_id)
            .order_by(BillingSubscription.id.desc())
        )
        return list(result.scalars().all())

    @classmethod
    def _current_subscription(cls, subs: Sequence[_Sub]) -> tuple[_Sub | None, bool]:
        """The subscription that answers "is this tenant entitled?".

        The newest subscription that grants access (same predicate as the live
        check) wins; otherwise the newest one. Returns ``(sub, grants)``.
        """
        deny_on_past_due = settings.billing_deny_on_past_due
        allow_intentless = settings.billing_grant_intentless_subscriptions
        for sub in subs:
            if subscription_grants_access(
                sub,
                deny_on_past_due=deny_on_past_due,
                allow_intentless=allow_intentless,
            ):
                return sub, True
        return (subs[0] if subs else None), False

    @classmethod
    async def _plan_info(
        cls, sub: BillingSubscription, db: AsyncSession
    ) -> PlanInfo | None:
        if sub.price_id is None:
            return None
        # One joined read (was: price, product + the prices selectin = 3).
        result = await db.execute(
            select(
                BillingPrice.id,
                BillingPrice.recurring_interval,
                BillingProduct.id.label("product_id"),
                BillingProduct.name,
            )
            .join(BillingProduct, BillingProduct.id == BillingPrice.product_id)
            .where(BillingPrice.id == sub.price_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return PlanInfo(
            product_id=row.product_id,
            price_id=row.id,
            name=row.name,
            interval=row.recurring_interval,
        )

    @classmethod
    async def get_subscription_status(
        cls, customer_id: UUID, db: AsyncSession
    ) -> SubscriptionOut:
        return await cls._status_of(await cls._subscriptions(customer_id, db), db)

    @classmethod
    async def _status_of(
        cls, subs: list[BillingSubscription], db: AsyncSession
    ) -> SubscriptionOut:
        sub, _ = cls._current_subscription(subs)
        if sub is None:
            return SubscriptionOut(status="none")
        return SubscriptionOut(
            status=sub.status,
            current_period_end=sub.current_period_end,
            trial_end=sub.trial_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            plan=await cls._plan_info(sub, db),
        )

    @classmethod
    async def tenant_subscriptions(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> list[BillingSubscription] | None:
        """The tenant's subscriptions (newest first) in ONE read.

        ``None`` when the tenant has no billing customer, ``[]`` when it has
        one without subscriptions — a customer LEFT JOIN subscriptions instead
        of a customer lookup followed by a subscriptions query.
        """
        result = await db.execute(
            select(BillingCustomer.id, BillingSubscription)
            .select_from(BillingCustomer)
            .outerjoin(
                BillingSubscription,
                BillingSubscription.customer_id == BillingCustomer.id,
            )
            .where(cls._tenant_match(tenant_type, tenant_id))
            .order_by(BillingSubscription.id.desc())
        )
        rows = result.all()
        if not rows:
            return None
        return [sub for _customer_id, sub in rows if sub is not None]

    @classmethod
    async def tenant_subscriptions_with_plans(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> tuple[list[BillingSubscription], dict[int, str]] | None:
        """:meth:`tenant_subscriptions` plus each subscription's plan name.

        The plan (price -> product, as :meth:`_plan_info` resolves it) is
        LEFT JOINed into the same statement: token claims need the current
        subscription's plan code, and a second SELECT for it was one more
        round trip per login / refresh of a subscribed user. Returns
        ``(subscriptions newest first, {subscription id: plan name})`` — a
        subscription without a price or whose price / product is unknown has
        no entry — or ``None`` without a billing customer.
        """
        result = await db.execute(
            cls._subscriptions_with_plans_statement(tenant_type),
            {"b_tenant_id": tenant_id},
        )
        rows = result.all()
        if not rows:
            return None
        subs: list[BillingSubscription] = []
        plans: dict[int, str] = {}
        for row in rows:
            sub: BillingSubscription | None = row[1]
            plan_name: str | None = row[2]
            if sub is None:
                continue
            subs.append(sub)
            if plan_name is not None:
                plans[sub.id] = plan_name
        return subs, plans

    #: tenant_type -> the prebuilt :meth:`tenant_subscriptions_with_plans`
    #: statement (the tenant id is bound per call as ``b_tenant_id``).
    _SUBSCRIPTIONS_WITH_PLANS: dict[str, _SubsWithPlans] = {}  # noqa: RUF012 - class-wide prebuilt statement cache

    @classmethod
    def _subscriptions_with_plans_statement(cls, tenant_type: str) -> _SubsWithPlans:
        cached = cls._SUBSCRIPTIONS_WITH_PLANS.get(tenant_type)
        if cached is None:
            cached = (
                select(BillingCustomer.id, BillingSubscription, BillingProduct.name)
                .select_from(BillingCustomer)
                .outerjoin(
                    BillingSubscription,
                    BillingSubscription.customer_id == BillingCustomer.id,
                )
                .outerjoin(
                    BillingPrice, BillingPrice.id == BillingSubscription.price_id
                )
                .outerjoin(BillingProduct, BillingProduct.id == BillingPrice.product_id)
                .where(cls._tenant_match(tenant_type, bindparam("b_tenant_id")))
                .order_by(BillingSubscription.id.desc())
            )
            cls._SUBSCRIPTIONS_WITH_PLANS[tenant_type] = cached
        return cached

    @staticmethod
    def _tenant_match(
        tenant_type: str, tenant_id: UUID | BindParameter[Any]
    ) -> ColumnElement[bool]:
        """The ``billing_customers`` predicate selecting one tenant's customer."""
        if tenant_type == "user":
            return BillingCustomer.user_id == tenant_id
        if tenant_type == "organization":
            return BillingCustomer.organization_id == tenant_id
        raise ValueError(f"unknown billing tenant_type {tenant_type!r}")

    @classmethod
    async def ensure_tenant_customer(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> BillingCustomer:
        """The tenant's customer (``user`` / ``organization``), created race-free."""
        return await cls._ensure_tenant_customer(tenant_type, tenant_id, db)

    @classmethod
    async def _ensure_tenant_customer(
        cls, tenant_type: str, tenant_id: UUID, db: AsyncSession
    ) -> BillingCustomer:
        if tenant_type == "user":
            return await cls.get_or_create_customer(db, user_id=tenant_id)
        return await cls.get_or_create_customer(db, organization_id=tenant_id)

    @classmethod
    async def tenant_subscription_status(
        cls, tenant_type: str, tenant_id: UUID, reads: Reads
    ) -> SubscriptionOut:
        """``GET /subscription`` for a resolved tenant: declared reads only.

        Never writes: a tenant WITHOUT a billing customer answers exactly what
        a freshly created customer answers (``status="none"``); the customer
        is created only by the paths that need one (checkout / portal
        intents). The subscriptions (newest first) and the current one's plan
        are declared operations (``services.operations``).
        """
        op = subscriptions_of[tenant_type]  # KeyError: unknown tenant kind
        subs: list[SubscriptionRow] = []
        cursor: str | None = None
        while True:
            page = await reads.page(
                op, limit=op.page_cap(), cursor=cursor, tenant_id=tenant_id
            )
            subs.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        sub, _ = cls._current_subscription(subs)
        if sub is None:
            return SubscriptionOut(status="none")
        plan = None
        if sub.price_id is not None:
            row = await reads.one(plan_of_price, id=sub.price_id)
            if row is not None:
                plan = PlanInfo(
                    product_id=row.product_id,
                    price_id=row.id,
                    name=row.name,
                    interval=row.recurring_interval,
                )
        return SubscriptionOut(
            status=sub.status,
            current_period_end=sub.current_period_end,
            trial_end=sub.trial_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            plan=plan,
        )

    @classmethod
    async def tenant_credit_balance(
        cls, tenant_type: str, tenant_id: UUID, reads: Reads
    ) -> int:
        """``GET /credits`` for a resolved tenant: one declared read.

        Never writes: no customer (or no balance row) is a balance of 0 —
        what a freshly created customer has.
        """
        row = await reads.one(balance_of[tenant_type], tenant_id=tenant_id)
        return 0 if row is None else int(row.amount)

    @classmethod
    async def subscription_claims(
        cls, customer_id: UUID, db: AsyncSession
    ) -> dict[str, object]:
        """Token claims computed with the SAME predicate as the live check.

        A claim may be stale (token TTL) but never disagrees with the live
        answer at mint time: a paying status that no longer grants access is
        emitted as ``expired``.
        """
        return await cls.claims_for_subscriptions(
            await cls._subscriptions(customer_id, db), db
        )

    @classmethod
    async def claims_for_subscriptions(
        cls,
        subs: list[BillingSubscription],
        db: AsyncSession,
        *,
        plan_names: dict[int, str] | None = None,
    ) -> dict[str, object]:
        """:meth:`subscription_claims` over already-loaded subscriptions.

        ``plan_names`` (from :meth:`tenant_subscriptions_with_plans`) supplies
        the plan already read with the subscriptions; without it the current
        subscription's plan is read separately.
        """
        sub, grants = cls._current_subscription(subs)
        if sub is None:
            return {"subscription_status": "none"}
        claim_status = sub.status
        if not grants and sub.status in (STATUS_ACTIVE, STATUS_TRIALING):
            claim_status = CLAIM_STATUS_EXPIRED
        if (
            not grants
            and sub.status == STATUS_PAST_DUE
            and not settings.billing_deny_on_past_due
        ):
            claim_status = CLAIM_STATUS_EXPIRED  # grace window elapsed
        claims: dict[str, object] = {"subscription_status": claim_status}
        if not grants:
            return claims
        if plan_names is not None:
            plan_name = plan_names.get(sub.id)
        else:
            plan = await cls._plan_info(sub, db)
            plan_name = plan.name if plan is not None else None
        if plan_name is not None:
            claims["plan_code"] = plan_name
        return claims

    @classmethod
    async def check_subscription_active(
        cls, customer_id: UUID, db: AsyncSession
    ) -> bool:
        """Access computation per spec §9 (incl. past_due grace policy)."""
        _, grants = cls._current_subscription(await cls._subscriptions(customer_id, db))
        return grants

    # --- Credits (idempotent ledger + materialized balance, spec §9) --------

    @classmethod
    async def get_credit_balance(cls, customer_id: UUID, db: AsyncSession) -> int:
        result = await db.execute(
            select(CreditBalance.amount).where(CreditBalance.customer_id == customer_id)
        )
        balance = result.scalar_one_or_none()
        return int(balance or 0)

    @staticmethod
    def validate_credit_operation(
        amount: object, operation_key: object, *, allow_zero: bool = False
    ) -> None:
        """``allow_zero``: a spend of 0 is a no-op (a free item); a grant of 0
        is still refused."""
        floor = 0 if allow_zero else 1
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount < floor
            or amount > MAX_CREDIT_OPERATION
        ):
            kind = "non-negative" if allow_zero else "positive"
            raise ValueError(
                f"amount must be a {kind} integer <= {MAX_CREDIT_OPERATION}"
            )
        if not isinstance(operation_key, str) or not 0 < len(operation_key) <= 255:
            raise ValueError("operation_key must be a 1..255 character string")

    @classmethod
    async def add_credits(
        cls,
        customer: BillingCustomer,
        amount: int,
        operation_key: str,
        db: AsyncSession,
        reason: str = "",
        *,
        channel: str = events.CREDITS_ADDED_V1,
    ) -> int:
        """Grant credits (``channel``: the event published — a won dispute's
        restoration is ``billing.credits.restored.v1``)."""
        cls.validate_credit_operation(amount, operation_key)
        customer_id = customer.id
        tenant_type = customer.tenant_type
        tenant_id = customer.tenant_id

        ledger_id = await cls._insert_ledger_idempotent(
            customer_id, amount, operation_key, reason, db
        )
        if ledger_id is None:
            # Idempotent replay: return the ORIGINAL result (or 409 if the key
            # was used for a different operation). DO NOTHING wrote nothing.
            return await cls._replay(customer_id, amount, operation_key, db)

        result = await db.execute(
            pg_insert(CreditBalance)
            .values(customer_id=customer_id, amount=amount)
            .on_conflict_do_update(
                index_elements=[CreditBalance.customer_id],
                set_={"amount": CreditBalance.amount + amount},
            )
            .returning(CreditBalance.amount)
        )
        new_balance = int(result.scalar_one())
        await cls._record_balance_after(ledger_id, new_balance, db)
        await events.billing_event(
            channel,
            customer_id,
            tenant_type,
            tenant_id,
            {"amount": amount, "balance": new_balance, "reason": reason},
        ).publish()
        return new_balance

    @classmethod
    async def spend_credits(
        cls,
        customer: BillingCustomer,
        amount: int,
        operation_key: str,
        db: AsyncSession,
        reason: str = "",
    ) -> int:
        cls.validate_credit_operation(amount, operation_key, allow_zero=True)
        customer_id = customer.id
        if amount == 0:
            # A free item (a zero price, a promo code) is still a business
            # fact: it goes through the same path — a ledger row with delta 0
            # under its operation key (so a replay with another amount is
            # 409) and the spent event — with the balance unchanged. The
            # balance row may not exist yet for a customer never credited.
            await db.execute(
                pg_insert(CreditBalance)
                .values(customer_id=customer_id, amount=0)
                .on_conflict_do_nothing(index_elements=[CreditBalance.customer_id])
            )
        tenant_type = customer.tenant_type
        tenant_id = customer.tenant_id

        # SAVEPOINT: the balance is debited FIRST (UPDATE … RETURNING gives the
        # resulting balance), then the ledger row is inserted WITH its
        # balance_after — two statements instead of insert + debit + a third
        # UPDATE of balance_after. A replayed operation key (ledger conflict)
        # or an overdraft rolls the savepoint back, undoing the debit, without
        # destroying the caller's (UoW-governed) transaction.
        new_balance: int | None = None
        ledger_id: int | None = None
        debit = update(CreditBalance).where(CreditBalance.customer_id == customer_id)
        if amount > 0:
            # Only a paid spend needs cover. A free one is recorded whatever
            # the balance: a refund or a lost dispute may leave it negative.
            debit = debit.where(CreditBalance.amount >= amount)
        try:
            async with db.begin_nested():
                result = await db.execute(
                    debit.values(amount=CreditBalance.amount - amount)
                    .returning(CreditBalance.amount)
                    .execution_options(synchronize_session=False)
                )
                new_balance = result.scalar_one_or_none()
                if new_balance is None:
                    raise _SpendNotApplied()
                ledger_id = await cls._insert_ledger_idempotent(
                    customer_id,
                    -amount,
                    operation_key,
                    reason,
                    db,
                    balance_after=int(new_balance),
                )
                if ledger_id is None:
                    raise _SpendNotApplied()  # replay: undo the debit
        except _SpendNotApplied:
            # Either an overdraft or an already-applied key. A replay must
            # return the ORIGINAL result even when the balance is now short,
            # so look the key up before reporting insufficient credits.
            if await cls._ledger_has(customer_id, operation_key, db):
                return await cls._replay(customer_id, -amount, operation_key, db)
            raise InsufficientCreditsError() from None

        if ledger_id is None or new_balance is None:  # pragma: no cover
            raise RuntimeError(
                "credit spend applied a ledger entry without a resulting balance"
            )
        await events.billing_event(
            events.CREDITS_SPENT_V1,
            customer_id,
            tenant_type,
            tenant_id,
            {"amount": amount, "balance": int(new_balance), "reason": reason},
        ).publish()
        return int(new_balance)

    @classmethod
    async def reverse_credits(
        cls,
        customer: BillingCustomer,
        amount: int,
        operation_key: str,
        db: AsyncSession,
        reason: str = "",
    ) -> int | None:
        """Take back ``amount`` credits whose payment was refunded / disputed
        (ADR-0003 open item 12): an append-only NEGATIVE ledger entry, durably
        deduped by ``operation_key`` in the caller's transaction. Unlike a
        spend it is never refused: the balance may go NEGATIVE (credits
        already spent are owed; later grants net it, spends are refused until
        then). Returns the new balance, or ``None`` when the key was already
        applied."""
        cls.validate_credit_operation(amount, operation_key)
        ledger_id = await cls._insert_ledger_idempotent(
            customer.id, -amount, operation_key, reason, db
        )
        if ledger_id is None:
            return None
        result = await db.execute(
            pg_insert(CreditBalance)
            .values(customer_id=customer.id, amount=-amount)
            .on_conflict_do_update(
                index_elements=[CreditBalance.customer_id],
                set_={"amount": CreditBalance.amount - amount},
            )
            .returning(CreditBalance.amount)
        )
        new_balance = int(result.scalar_one())
        await cls._record_balance_after(ledger_id, new_balance, db)
        await events.billing_event(
            events.CREDITS_REVERSED_V1,
            customer.id,
            customer.tenant_type,
            customer.tenant_id,
            {"amount": amount, "balance": new_balance, "reason": reason},
        ).publish()
        return new_balance

    @classmethod
    async def _insert_ledger_idempotent(
        cls,
        customer_id: UUID,
        delta: int,
        operation_key: str,
        reason: str,
        db: AsyncSession,
        *,
        balance_after: int | None = None,
    ) -> int | None:
        """INSERT … ON CONFLICT (customer_id, operation_key) DO NOTHING.

        Returns the new ledger row id, or ``None`` when the key already exists
        for this customer.
        """
        result = await db.execute(
            pg_insert(CreditLedgerEntry)
            .values(
                customer_id=customer_id,
                delta=delta,
                operation_key=operation_key,
                reason=reason,
                balance_after=balance_after,
            )
            # UNIQUE operation key on the ledger row itself
            .on_conflict_do_nothing(
                index_elements=[
                    CreditLedgerEntry.customer_id,
                    CreditLedgerEntry.operation_key,
                ]
            )
            .returning(CreditLedgerEntry.id)
        )
        inserted = result.scalar_one_or_none()
        return int(inserted) if inserted is not None else None

    @classmethod
    async def _record_balance_after(
        cls, ledger_id: int, balance: int, db: AsyncSession
    ) -> None:
        await db.execute(
            update(CreditLedgerEntry)
            .where(CreditLedgerEntry.id == ledger_id)
            .values(balance_after=balance)
            .execution_options(synchronize_session=False)
        )

    @classmethod
    async def _ledger_has(
        cls, customer_id: UUID, operation_key: str, db: AsyncSession
    ) -> bool:
        result = await db.execute(
            select(CreditLedgerEntry.id).where(
                CreditLedgerEntry.customer_id == customer_id,
                CreditLedgerEntry.operation_key == operation_key,
            )
        )
        return result.first() is not None

    @classmethod
    async def _replay(
        cls, customer_id: UUID, delta: int, operation_key: str, db: AsyncSession
    ) -> int:
        """Result of an already-applied operation, or 409 if it differs."""
        result = await db.execute(
            select(CreditLedgerEntry.delta, CreditLedgerEntry.balance_after).where(
                CreditLedgerEntry.customer_id == customer_id,
                CreditLedgerEntry.operation_key == operation_key,
            )
        )
        row = result.one()
        if int(row.delta) != delta:
            raise CreditOperationConflictError(operation_key)
        if row.balance_after is None:  # legacy row (pre-0002): best effort
            return await cls.get_credit_balance(customer_id, db)
        return int(row.balance_after)

    # --- PSP sessions ---------------------------------------------------------

    @classmethod
    async def create_checkout_session(
        cls,
        customer: BillingCustomer,
        price_id: int,
        psp: StripeClientPort,
        db: AsyncSession,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Checkout in ONE transaction (no operation intent).

        The HTTP route uses the intent flow instead
        (:mod:`sandloom_billing.services.operation_intents`, ADR-0003 G1); this
        stays for direct callers and tests of the PSP metadata.
        """
        plan = await cls.checkout_plan(price_id, db)
        local_customer_id = customer.id
        external_customer = await cls.ensure_psp_customer(customer, psp, db)
        session = await cls.start_checkout(
            psp,
            plan,
            customer_id=local_customer_id,
            external_customer=external_customer,
            idempotency_key=cls._psp_idempotency_key(
                "checkout", idempotency_key, local_customer_id, plan.price_id
            ),
        )
        return session.url

    @classmethod
    async def checkout_plan(cls, price_id: int, db: AsyncSession) -> CheckoutPlan:
        """What a checkout of ``price_id`` buys; 404 for an unknown price or an
        inactive product."""
        result = await db.execute(
            select(BillingPrice).where(BillingPrice.id == price_id)
        )
        price = result.scalar_one_or_none()
        if price is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Price not found"
            )
        product_result = await db.execute(
            select(BillingProduct).where(BillingProduct.id == price.product_id)
        )
        product = product_result.scalar_one_or_none()
        if product is None or not product.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Product not available"
            )
        return CheckoutPlan(
            price_id=price.id,
            external_price_id=price.stripe_price_id or f"local_price_{price.id}",
            mode=(
                "subscription"
                if product.type == PRODUCT_TYPE_SUBSCRIPTION
                else "payment"
            ),
        )

    @staticmethod
    async def start_checkout(
        psp: StripeClientPort,
        plan: CheckoutPlan,
        *,
        customer_id: UUID,
        external_customer: str,
        idempotency_key: str | None,
        intent_metadata: dict[str, str] | None = None,
    ) -> PspSession:
        """The PSP call of a checkout (no database access)."""
        frontend = settings.frontend_url.rstrip("/")
        # Metadata identifies WHAT was bought (our price id) — never how many
        # credits: the webhook resolves the grant from the catalog row.
        return await psp.create_checkout_session(
            customer_id=external_customer,
            price_external_id=plan.external_price_id,
            success_url=f"{frontend}/billing/success",
            cancel_url=f"{frontend}/billing/cancel",
            mode=plan.mode,
            metadata={
                "customer_id": str(customer_id),
                "price_id": str(plan.price_id),
                **(intent_metadata or {}),
            },
            subscription_metadata=intent_metadata,
            idempotency_key=idempotency_key,
        )

    @classmethod
    async def create_customer_portal_session(
        cls,
        customer: BillingCustomer,
        psp: StripeClientPort,
        db: AsyncSession,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        local_customer_id = customer.id
        external_customer = await cls.ensure_psp_customer(customer, psp, db)
        return await cls.start_customer_portal(
            psp,
            external_customer=external_customer,
            idempotency_key=cls._psp_idempotency_key(
                "customer_portal", idempotency_key, local_customer_id
            ),
        )

    @staticmethod
    async def start_customer_portal(
        psp: StripeClientPort, *, external_customer: str, idempotency_key: str | None
    ) -> str:
        """The PSP call of a customer-portal session (no database access)."""
        frontend = settings.frontend_url.rstrip("/")
        return await psp.create_billing_portal_session(
            customer_id=external_customer,
            return_url=f"{frontend}/billing",
            idempotency_key=idempotency_key,
        )

    # --- Helpers --------------------------------------------------------------

    @staticmethod
    def _psp_idempotency_key(
        operation: str, request_key: str | None, *scope_parts: object
    ) -> str | None:
        if request_key is None:
            return None
        scope = "|".join(str(part) for part in scope_parts)
        digest = hashlib.sha256(f"{scope}|{request_key}".encode()).hexdigest()
        return f"sandloom:{operation}:{digest}"

    @staticmethod
    def parse_ts(value: object) -> datetime | None:
        """Parse a Stripe unix timestamp into aware datetime."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(value, tz=UTC)
        return None
