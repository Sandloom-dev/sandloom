"""Operation-authorization model of billing's class-F routes (ADR-0003 G1).

A checkout / customer-portal session is an effect outside any database: the
PSP call cannot be rolled back, and billing's database may not be the
database that holds the authority (tenancy's membership row, identity's
session marks). The route therefore runs as a sequence of SHORT, separately
committed Units of Work around the PSP call — none of them open while the PSP
is called, none nested in another:

1. **authorize** (before this module): live ``get_member_state`` — role and
   membership ``version`` — for an organization; the caller's own session
   (strict revocation / block marks) for the personal tenant.
2. **reserve** (UoW 1, committed): validate the price, get-or-create the
   customer, and persist the authorized :class:`OperationIntent` (``pending``)
   — or find the live intent of a retry of the same request (same
   Idempotency-Key, same parameters). Committed BEFORE the PSP call, so a crash
   or a timeout leaves a record to reconcile with.
3. **PSP customer** (only once per tenant): PSP call, then UoW 2 links it.
4. **PSP session**, keyed ``sandloom:intent:<intent id>``: a retry of a pending
   intent re-sends the SAME key and gets the SAME session; it is never a blind
   new call with a fresh key. The intent id travels in the session's (and its
   subscription's) metadata for the webhook gate.
5. **finalize**: the final re-read — the authority (``reverify_authority``:
   403 lost / 409 changed), then the caller's session (``subject_commit_check``:
   401) — with no billing connection held, then UoW 3 marks the PENDING intent
   ``issued`` in one statement. Any refusal instead marks the PENDING intent
   ``revoked`` (UoW 4, audited) and EXPIRES the checkout
   session at the PSP; the URL is never returned. A portal session cannot be
   expired (Stripe has no such call): it is simply never handed out.
   ANY failure of the re-check (owner unreachable, fail-closed revocation
   store outage, provider error) fails closed the same way.
6. **retries**: the routes' idempotency guard stores no response (in-flight
   dedupe only), so a retry re-authorizes and reaches the intent: pending →
   reconciled with the same PSP key; issued checkout → no PSP call, a fresh
   re-read, the stored URL; issued portal → 409. Nothing revokes, fails or
   rewrites an issued intent (every transition is ``WHERE status =
   'pending'``).

Authorization point: the issue COMMIT. Authority is checked against tenancy's
committed state AS OF THE FINAL RE-READ: the membership ``version`` captured at
authorize is compared with a live read of tenancy's state (no billing
transaction open, no lock in tenancy), then the session marks are read, then
the issue UPDATE + COMMIT runs. A change
committed in tenancy after that read and before billing's COMMIT is not seen.
All PSP calls of one intent share its key, so a retry cannot duplicate the
session (within the PSP's idempotency window, ~24 h at Stripe). Guarantee:
serializable, not linearizable — a withdrawal committing between the final
re-check and the issue COMMIT is not detected (ADR-0003 "Guarantees,
precisely").

Why ``open_uow()`` and not the request's ``DbSession``: the intent must
outlive a failed request (a revoked intent is the record the webhook gate and
the operator need), so it cannot live in the request transaction, which rolls
back on the very ``HTTPException`` that reports the refusal. AGENTS.md's rule
"a write that must survive the request rollback needs its own transaction —
never nested inside it" holds: these routes take NO request session, so every
transaction here is the only one the request holds (no second pooled
connection while one is checked out), and no transaction spans the PSP call
(no locks or connections held across seconds of HTTP).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom.sdk.data import open_uow
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.web import ResolvedAccount, external_call, release_check
from sandloom_billing.models import (
    INTENT_FAILED,
    INTENT_ISSUED,
    INTENT_METADATA_KEY,
    INTENT_PENDING,
    INTENT_REVOKED,
    LIVE_INTENT_STATUSES,
    OPERATION_CHECKOUT,
    OPERATION_CUSTOMER_PORTAL,
    BillingCustomer,
    OperationIntent,
)
from sandloom_billing.psp import (
    PspSession,
    StripeApiError,
    StripeClientPort,
    StripeRequestRejected,
)
from sandloom_billing.services.billing_service import BillingService, CheckoutPlan
from sandloom_billing.services.tenant_resolver import TenantContext, reverify_authority

logger = logging.getLogger("sandloom_billing.intents")

_T = TypeVar("_T")

#: Counter (``sandloom_facade_counter_total{name=...}``): a refused checkout session
#: could not be expired at the PSP (the compensation failed).
EXPIRE_FAILED_METRIC = "billing_intent_expire_failed"

#: Audit action of an intent refused after its PSP call.
AUDIT_INTENT_REVOKED = "billing.intent.revoked"

#: The PSP's default checkout session lifetime (Stripe: 24 h; billing never
#: sets ``expires_at``). Applies to intents issued before the expiry was
#: stored (ADR-0003 open item 13).
PSP_DEFAULT_SESSION_LIFETIME = timedelta(hours=24)

#: A stored checkout URL is not handed out when it would die within this.
SESSION_EXPIRY_MARGIN = timedelta(minutes=1)


def psp_key_for(intent_id: UUID) -> str:
    """The PSP idempotency key of an intent (stable across retries)."""
    return f"sandloom:intent:{intent_id}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _Reserved:
    """What the PSP steps need from the reserve transaction (plain values)."""

    intent_id: UUID
    customer_id: UUID
    tenant_type: str
    tenant_id: UUID
    external_customer: str | None
    plan: CheckoutPlan | None
    #: The intent's status when reserved: ``pending`` (new or a retry to
    #: reconcile) or ``issued`` (a retry after the URL was already issued).
    status: str = INTENT_PENDING
    #: The stored checkout URL of an ``issued`` intent (never a portal URL).
    session_url: str | None = None
    #: When that URL dies (stored PSP expiry; else created + PSP default).
    session_expires_at: datetime | None = None

    @property
    def psp_key(self) -> str:
        return psp_key_for(self.intent_id)


class OperationIntentService:
    """Runs a class-F PSP operation under an authorized, persisted intent."""

    @classmethod
    @sensitive_effect("money")
    async def checkout(
        cls,
        *,
        core: Any,
        request: Request,
        ctx: TenantContext,
        price_id: int,
        psp: StripeClientPort,
        request_key: str | None,
    ) -> str:
        """Checkout session URL for ``ctx`` (see the module docstring)."""
        reserved = await cls._reserve(OPERATION_CHECKOUT, ctx, price_id, request_key)
        if reserved.status == INTENT_ISSUED:
            # A retry after the URL was issued (response or guard record lost):
            # NEVER call the PSP again (after its key window that would be a
            # new session) and never touch the intent. Re-check the authority
            # afresh and return the stored URL of the same session; a failed
            # re-check refuses THIS request only — the issued intent and its
            # session stand (nothing revokes an issued intent).
            await cls._recheck(core, request, ctx)
            assert reserved.session_url is not None  # noqa: S101 - type narrowing of an invariant
            expires_at = reserved.session_expires_at
            if expires_at is not None and (
                expires_at - SESSION_EXPIRY_MARGIN <= datetime.now(UTC)
            ):
                # Never hand out a dead URL (ADR-0003 open item 13). The
                # intent stays issued (terminal); a new checkout needs a new
                # Idempotency-Key (a new intent).
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail=(
                        "This checkout session has expired; start a new "
                        "checkout with a new Idempotency-Key"
                    ),
                )
            return reserved.session_url
        external = await cls._psp_customer(psp, reserved)
        plan = reserved.plan
        assert plan is not None  # noqa: S101 - type narrowing of an invariant
        session = await cls._call_psp(
            reserved,
            BillingService.start_checkout(
                psp,
                plan,
                customer_id=reserved.customer_id,
                external_customer=external,
                idempotency_key=reserved.psp_key,
                intent_metadata={INTENT_METADATA_KEY: str(reserved.intent_id)},
            ),
        )
        await cls._finalize(core, request, ctx, reserved, psp, session=session)
        return session.url

    @classmethod
    @sensitive_effect("money")
    async def customer_portal(
        cls,
        *,
        core: Any,
        request: Request,
        ctx: TenantContext,
        psp: StripeClientPort,
        request_key: str | None,
    ) -> str:
        """Customer-portal URL for ``ctx``. The URL is never stored (a bearer
        credential to the payment settings) and the session cannot be expired:
        refused means not handed out."""
        reserved = await cls._reserve(OPERATION_CUSTOMER_PORTAL, ctx, None, request_key)
        if reserved.status == INTENT_ISSUED:
            # The portal URL is never stored, so it cannot be returned again,
            # and a new PSP call is a new session: refuse the retry.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This customer-portal request was already completed; open "
                    "the portal again with a new Idempotency-Key"
                ),
            )
        external = await cls._psp_customer(psp, reserved)
        url = await cls._call_psp(
            reserved,
            BillingService.start_customer_portal(
                psp, external_customer=external, idempotency_key=reserved.psp_key
            ),
        )
        await cls._finalize(core, request, ctx, reserved, psp, session=None)
        return url

    # --- 2. reserve -----------------------------------------------------------

    @classmethod
    async def _reserve(
        cls,
        operation: str,
        ctx: TenantContext,
        price_id: int | None,
        request_key: str | None,
    ) -> _Reserved:
        params_digest = _sha256(
            f"{operation}|{ctx.tenant_type}|{ctx.tenant_id}|{price_id or ''}"
        )
        hashed_key = _sha256(request_key) if request_key else None
        version = ctx.authority.version if ctx.authority is not None else None
        async with open_uow() as db:
            plan = (
                await BillingService.checkout_plan(price_id, db)
                if price_id is not None
                else None
            )
            customer = await BillingService.ensure_tenant_customer(
                ctx.tenant_type, ctx.tenant_id, db
            )
            intent = None
            if hashed_key is not None:
                intent = await cls._live_intent(db, ctx.user_id, operation, hashed_key)
            if intent is None:
                intent = await cls._insert(
                    db,
                    operation=operation,
                    ctx=ctx,
                    customer=customer,
                    hashed_key=hashed_key,
                    params_digest=params_digest,
                    price_id=price_id,
                    version=version,
                )
            # Owner check on the retry lookup (actor-scoped by the query):
            # the live intent must belong to THIS tenant's customer and these
            # parameters; the same actor's key on another tenant is refused.
            if (
                intent.customer_id != customer.id
                or intent.params_digest != params_digest
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Idempotency-Key reused with different parameters",
                )
            return _Reserved(
                intent_id=intent.id,
                customer_id=customer.id,
                tenant_type=ctx.tenant_type,
                tenant_id=ctx.tenant_id,
                external_customer=customer.stripe_customer_id,
                plan=plan,
                status=intent.status,
                session_url=(
                    intent.psp_session_url if intent.status == INTENT_ISSUED else None
                ),
                session_expires_at=(
                    intent.psp_session_expires_at
                    or intent.created_at + PSP_DEFAULT_SESSION_LIFETIME
                    if intent.status == INTENT_ISSUED
                    else None
                ),
            )

    @staticmethod
    async def _live_intent(
        db: AsyncSession, actor_id: UUID, operation: str, hashed_key: str
    ) -> OperationIntent | None:
        result = await db.execute(
            select(OperationIntent).where(
                OperationIntent.actor_id == actor_id,
                OperationIntent.operation == operation,
                OperationIntent.request_key == hashed_key,
                OperationIntent.status.in_(LIVE_INTENT_STATUSES),
            )
        )
        return result.scalar_one_or_none()

    @classmethod
    async def _insert(
        cls,
        db: AsyncSession,
        *,
        operation: str,
        ctx: TenantContext,
        customer: BillingCustomer,
        hashed_key: str | None,
        params_digest: str,
        price_id: int | None,
        version: int | None,
    ) -> OperationIntent:
        intent_id = uuid4()
        inserted = await db.execute(
            pg_insert(OperationIntent)
            .values(
                id=intent_id,
                operation=operation,
                status=INTENT_PENDING,
                customer_id=customer.id,
                tenant_type=ctx.tenant_type,
                tenant_id=ctx.tenant_id,
                actor_id=ctx.user_id,
                request_key=hashed_key,
                params_digest=params_digest,
                price_id=price_id,
                authorized_version=version,
                psp_idempotency_key=psp_key_for(intent_id),
            )
            # UNIQUE request key on the intent row itself
            .on_conflict_do_nothing(
                index_elements=["actor_id", "operation", "request_key"],
                index_where=OperationIntent.status.in_(LIVE_INTENT_STATUSES),
            )
            .returning(OperationIntent.id)
        )
        new_id = inserted.scalar_one_or_none()
        if new_id is None and hashed_key is not None:
            # A concurrent request with the same key won the insert.
            existing = await cls._live_intent(db, ctx.user_id, operation, hashed_key)
            if existing is not None:
                return existing
        stored = await db.get(OperationIntent, intent_id) if new_id else None
        if stored is None:  # pragma: no cover - lost a race that then closed
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A concurrent request with this Idempotency-Key; retry",
            )
        return stored

    # --- 3./4. PSP --------------------------------------------------------------

    @classmethod
    async def _psp_customer(cls, psp: StripeClientPort, reserved: _Reserved) -> str:
        if reserved.external_customer:
            return reserved.external_customer
        external = await cls._call_psp(
            reserved,
            BillingService.create_psp_customer(
                psp, reserved.tenant_type, reserved.tenant_id
            ),
        )
        async with open_uow() as db:
            return await BillingService.link_psp_customer(
                reserved.customer_id, external, db
            )

    @classmethod
    async def _call_psp(cls, reserved: _Reserved, call: Awaitable[_T]) -> _T:
        """Await a PSP call. A definitive rejection fails the intent (502); an
        ambiguous failure (timeout, 5xx, network) leaves it ``pending`` — the
        retry reconciles with the same key (503)."""
        try:
            async with external_call("psp"):
                return await call
        except StripeRequestRejected as exc:
            await cls._close(reserved, INTENT_FAILED, reason=str(exc)[:500])
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The payment provider rejected the request",
            ) from exc
        except (StripeApiError, TimeoutError, OSError) as exc:
            logger.warning(
                "billing intent %s: PSP call failed (%s); left pending for a retry "
                "with the same key",
                reserved.intent_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "The payment provider is unavailable; retry with the same "
                    "Idempotency-Key"
                ),
            ) from exc

    # --- 5. finalize ------------------------------------------------------------

    @staticmethod
    async def _recheck(core: Any, request: Request, ctx: TenantContext) -> None:
        """The final re-read, ``release_check`` (spec §4.6): tenancy's
        membership state (``version`` vs the one captured at authorize: 403
        lost / 409 changed), then the caller's subject-state record (401).
        Runs OUTSIDE any billing transaction (the kernel enforces it: no unit
        or connection checkout can start meanwhile)."""

        async def _authority() -> None:
            await reverify_authority(core, ctx)

        account = ResolvedAccount(
            kind="org" if ctx.tenant_type == "organization" else "user",
            id=ctx.tenant_id,
        )
        await release_check(account, authority=_authority)

    @classmethod
    async def _finalize(
        cls,
        core: Any,
        request: Request,
        ctx: TenantContext,
        reserved: _Reserved,
        psp: StripeClientPort,
        *,
        session: PspSession | None,
    ) -> None:
        session_id = session.id if session is not None else None
        try:
            # The final re-read, then immediately a one-statement issue
            # transaction (UPDATE ... WHERE status = 'pending'; COMMIT). The
            # authorization point is that COMMIT; the window it leaves —
            # serializable, not linearizable — is final re-read -> COMMIT.
            await cls._recheck(core, request, ctx)
            async with open_uow() as db:
                issued = await db.execute(
                    update(OperationIntent)
                    .where(
                        OperationIntent.id == reserved.intent_id,
                        OperationIntent.customer_id == reserved.customer_id,
                        OperationIntent.actor_id == ctx.user_id,
                        OperationIntent.status == INTENT_PENDING,
                    )
                    .values(
                        status=INTENT_ISSUED,
                        issued_at=func.now(),
                        psp_session_id=session_id,
                        psp_session_url=session.url if session is not None else None,
                        psp_session_expires_at=(
                            session.expires_at if session is not None else None
                        ),
                        authorized_version=(
                            ctx.authority.version if ctx.authority is not None else None
                        ),
                    )
                    .returning(OperationIntent.id)
                    .execution_options(synchronize_session=False)
                )
                if issued.scalar_one_or_none() is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The operation is no longer pending; retry",
                    )
        except Exception as refusal:
            # ANY failure before the issue commit fails closed: a refusal
            # (401/403/409), the owner unreachable (503: RemoteServiceUnavailable,
            # a fail-closed revocation-store outage) or a provider error. The
            # authority could not be confirmed, so the intent is never issued.
            if isinstance(refusal, HTTPException):
                reason = f"{refusal.status_code}: {refusal.detail}"
            else:
                reason = f"re-check failed: {type(refusal).__name__}"
            issued_elsewhere = False
            try:
                revoked = await cls._close(
                    reserved,
                    INTENT_REVOKED,
                    reason=reason[:500],
                    session_id=session_id,
                    actor_id=ctx.user_id,
                )
                if not revoked:
                    # No longer pending: issued by a concurrent retry of the
                    # same request (same PSP key = the same session — never
                    # expire it), or closed by the reconciler meanwhile (#72:
                    # the session is not issued — expire it).
                    issued_elsewhere = await cls._is_issued(reserved)
            except Exception:  # billing DB down too: stays pending
                logger.error(
                    "billing intent %s: could not record the revocation; it stays "
                    "pending (never issued, the webhook gate refuses it)",
                    reserved.intent_id,
                    exc_info=True,
                )
            # Expire only a session whose intent is not issued.
            if session_id is not None and not issued_elsewhere:
                await cls._expire(core, psp, session_id, reserved)
            raise

    @staticmethod
    async def _is_issued(reserved: _Reserved) -> bool:
        async with open_uow() as db:
            current = await db.execute(
                select(OperationIntent.status).where(
                    OperationIntent.id == reserved.intent_id,
                    OperationIntent.customer_id == reserved.customer_id,
                )
            )
            return current.scalar_one_or_none() == INTENT_ISSUED

    @staticmethod
    async def _expire(
        core: Any, psp: StripeClientPort, session_id: str, reserved: _Reserved
    ) -> None:
        """Expire a refused checkout session — a COMPENSATION, not atomic with
        anything: it runs after the revocation committed and may itself fail.

        Not retried in the request (the refusal already stands: the intent is
        revoked and the URL withheld). The outcome is recorded on the intent:
        success sets ``psp_expired_at``; a failure is logged, counted
        (``billing_intent_expire_failed`` — alert on it) and recorded as a
        failed reconcile attempt, which the periodic reconciler retries
        (bounded, then ``followup_at``; #72). Until then the session stays
        payable at the PSP (Stripe: ≤ 24 h) and the webhook gate refuses its
        completion (audited, operator follow-up).
        """
        error: str | None = None
        try:
            async with external_call("psp"):
                await psp.expire_checkout_session(session_id)
        except Exception as exc:  # the gate is the backstop
            core.increment_metric(EXPIRE_FAILED_METRIC)
            error = f"expire failed: {type(exc).__name__}: {exc}"[:500]
            # The session id stays in the audit row only (not in logs).
            logger.error(
                "billing intent %s: could not expire its checkout session; the "
                "reconciler retries it, the webhook gate refuses its completion",
                reserved.intent_id,
                exc_info=True,
            )
        try:
            await record_expire_outcome(
                reserved.intent_id, reserved.customer_id, error=error
            )
        except Exception:  # billing DB down: logged only
            logger.error(
                "billing intent %s: could not record the expire outcome",
                reserved.intent_id,
                exc_info=True,
            )

    @classmethod
    async def _close(
        cls,
        reserved: _Reserved,
        outcome: str,
        *,
        reason: str,
        session_id: str | None = None,
        actor_id: UUID | None = None,
    ) -> bool:
        """Mark a PENDING intent ``revoked`` / ``failed`` in its own transaction
        (it must survive the request's failure); a revocation is audited.

        ``WHERE status = 'pending'``: an ``issued`` intent is never revoked or
        failed. Returns whether the intent was closed by this call.
        """
        return await close_pending_intent(
            intent_id=reserved.intent_id,
            customer_id=reserved.customer_id,
            tenant_type=reserved.tenant_type,
            tenant_id=reserved.tenant_id,
            outcome=outcome,
            reason=reason,
            session_id=session_id,
            actor_id=actor_id,
        )


async def close_pending_intent(
    *,
    intent_id: UUID,
    customer_id: UUID,
    tenant_type: str,
    tenant_id: UUID,
    outcome: str,
    reason: str,
    session_id: str | None = None,
    actor_id: UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """THE ``pending`` → ``revoked`` / ``failed`` transition, in its own
    transaction (it must survive the request's failure); a revocation is
    audited (system actor when ``actor_id`` is ``None``: the reconciler).

    ``WHERE status = 'pending'`` (+ owner): an ``issued`` intent is never
    revoked or failed. Returns whether the intent was closed by this call.
    ``extra`` adds non-status columns (reconciler: ``psp_expired_at``,
    ``followup_at``).
    """
    async with open_uow() as db:
        values: dict[str, Any] = {"status": outcome, "reason": reason, **(extra or {})}
        if session_id is not None:
            values["psp_session_id"] = session_id
        closed = await db.execute(
            update(OperationIntent)
            .where(
                OperationIntent.id == intent_id,
                OperationIntent.customer_id == customer_id,
                OperationIntent.status == INTENT_PENDING,
            )
            .values(**values)
            .returning(OperationIntent.id)
            .execution_options(synchronize_session=False)
        )
        if closed.scalar_one_or_none() is None:
            return False
        if outcome == INTENT_REVOKED:
            record_audit(
                AUDIT_INTENT_REVOKED,
                actor_id=actor_id,
                actor_type="user" if actor_id is not None else "system",
                resource_type="billing_intent",
                resource_id=str(intent_id),
                data={
                    "tenant_type": tenant_type,
                    "tenant_id": str(tenant_id),
                    "psp_session_id": session_id,
                    "reason": reason,
                },
                required=True,
            )
        return True


async def record_expire_outcome(
    intent_id: UUID, customer_id: UUID, *, error: str | None
) -> None:
    """Record the expire compensation of a REVOKED intent's checkout session:
    success → ``psp_expired_at``; failure → one more failed reconcile attempt
    and the error (the reconciler retries it). Never touches another status."""
    async with open_uow() as db:
        values: dict[str, Any]
        if error is None:
            values = {"psp_expired_at": datetime.now(UTC), "reconcile_error": None}
        else:
            values = {
                "reconcile_attempts": OperationIntent.reconcile_attempts + 1,
                "reconcile_error": error,
            }
        await db.execute(
            update(OperationIntent)
            .where(
                OperationIntent.id == intent_id,
                OperationIntent.customer_id == customer_id,
                OperationIntent.status == INTENT_REVOKED,
                # Once expired, stays expired: a late failure report (a
                # concurrent attempt) is not recorded.
                OperationIntent.psp_expired_at.is_(None),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )


__all__ = [
    "OperationIntentService",
    "PspSession",
    "close_pending_intent",
    "psp_key_for",
    "record_expire_outcome",
]
