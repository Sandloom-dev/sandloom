"""Periodic reconciliation of operation intents (#72; ADR-0003 open items 7, 15).

The request path (``operation_intents``) leaves two loose ends that no request
closes:

(a) a ``pending`` intent older than ``BILLING_INTENT_RECONCILE_AFTER_MINUTES``
    — its PSP answer was lost (timeout / 5xx / crash) and the client never
    retried, or the retry was refused before reaching it (authority lost:
    open item 15). Billing never learned the session id; the session may
    exist and be payable by whoever holds its URL (billing never returned
    it). The reconciler LISTS the intent customer's checkout sessions at the
    PSP, matches the intent marker (``metadata.sandloom_intent_id``) and:

    ========================  ==============================================
    PSP shows                 action
    ========================  ==============================================
    no session                ``pending`` → ``failed`` (nothing to expire)
    open session(s)           ``pending`` → ``revoked`` (audited), then
                              expire each (compensation, outcome recorded)
    expired session(s)        ``pending`` → ``revoked``, ``psp_expired_at``
    a completed session       ``pending`` → ``revoked`` + ``followup_at``
                              (paid without an issued URL; the webhook gate
                              refuses it; refund is manual)
    a portal intent           ``pending`` → ``revoked`` (a portal session
                              cannot be listed or expired; never handed out)
    PSP lookup fails          stays ``pending``; attempt + error recorded
    ========================  ==============================================

(b) a ``revoked`` checkout intent whose expire compensation FAILED in the
    request (``reconcile_attempts > 0``, ``psp_expired_at`` NULL): the
    reconciler looks the session up and expires it (or records that the PSP
    already expired it, or flags a completed one for an operator).

Invariants (the state model stays small and checkable):

- Only the existing transitions are used: ``pending`` → ``revoked`` /
  ``failed`` through :func:`close_pending_intent` (``WHERE status =
  'pending'``). An ``issued`` intent is never selected and never written.
- Every PSP failure is bounded: ``reconcile_attempts`` / ``reconcile_error``
  are persisted per intent; when the attempts reach
  ``BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`` the intent gets ``followup_at``
  and is no longer selected — an operator takes over.
- No transaction spans a PSP call: candidates are read in one short UoW, each
  write is its own UoW, all guarded by status + owner (``id`` +
  ``customer_id``). Billing tables have no RLS: the reconciler is system code
  scanning across tenants, but every PSP lookup is by the intent's OWN PSP
  customer and every write is constrained by the intent's own customer.
- Idempotent: a re-run (overlapping beat, ``acks_late`` redelivery) finds the
  intents already closed (status guard) or the sessions already expired (the
  PSP expire is idempotent); the worst case of a concurrent run is one extra
  attempt counted.

Signals: the run returns a :class:`ReconcileReport` (the Celery task result
and one log line — WARNING when anything is still failing or escalated). The
durable signal for alerting is the table itself (``reconcile_attempts``,
``followup_at``); the framework's Celery worker exports no Prometheus
endpoint, so no counter is incremented here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, select, update

from sandloom.sdk.data import open_uow
from sandloom.sdk.infra import settings
from sandloom_billing.models import (
    INTENT_FAILED,
    INTENT_METADATA_KEY,
    INTENT_PENDING,
    INTENT_REVOKED,
    OPERATION_CHECKOUT,
    BillingCustomer,
    OperationIntent,
)
from sandloom_billing.psp import (
    CHECKOUT_COMPLETE,
    CHECKOUT_EXPIRED,
    CHECKOUT_OPEN,
    CheckoutSessionInfo,
    StripeClientPort,
)
from sandloom_billing.services.operation_intents import (
    PSP_DEFAULT_SESSION_LIFETIME,
    close_pending_intent,
    record_expire_outcome,
)

logger = logging.getLogger("sandloom_billing.intents.reconcile")

#: Sessions are listed from this long before the intent was created (clock
#: skew between billing's database and the PSP).
_LOOKUP_SKEW = timedelta(minutes=5)

#: Intents handled per run and kind (the next run continues).
DEFAULT_BATCH = 100


@dataclass
class ReconcileReport:
    """Outcome counts of one run."""

    #: stale ``pending`` intents closed (``revoked`` / ``failed``)
    reconciled: int = 0
    #: owed session expiries done (expired by us, or found expired)
    compensated: int = 0
    #: a PSP step failed this run; retried by the next run
    still_failing: int = 0
    #: attempts exhausted this run: ``followup_at`` set, left to an operator
    escalated: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: UUID
    customer_id: UUID
    tenant_type: str
    tenant_id: UUID
    operation: str
    status: str
    external_customer: str | None
    created_at: datetime
    session_id: str | None
    session_expires_at: datetime | None


class IntentReconciler:
    """See the module docstring."""

    @classmethod
    async def run(
        cls,
        psp: StripeClientPort,
        *,
        now: datetime | None = None,
        stale_after: timedelta | None = None,
        max_attempts: int | None = None,
        batch: int = DEFAULT_BATCH,
    ) -> ReconcileReport:
        now = now or datetime.now(UTC)
        if stale_after is None:
            stale_after = timedelta(
                minutes=settings.billing_intent_reconcile_after_minutes
            )
        if max_attempts is None:
            max_attempts = settings.billing_intent_reconcile_max_attempts
        report = ReconcileReport()
        for candidate in await cls._stale_pending(
            now - stale_after, max_attempts, batch
        ):
            await cls._reconcile_pending(psp, candidate, now, max_attempts, report)
        for candidate in await cls._owed_expiries(max_attempts, batch):
            await cls._retry_expire(psp, candidate, now, max_attempts, report)
        return report

    # --- selection (one short read-only UoW each) ------------------------------

    @staticmethod
    async def _select(*conditions: Any, batch: int) -> list[_Candidate]:
        async with open_uow() as db:
            rows = await db.execute(
                select(OperationIntent, BillingCustomer.stripe_customer_id)
                .join(
                    BillingCustomer, BillingCustomer.id == OperationIntent.customer_id
                )
                .where(*conditions)
                .order_by(OperationIntent.created_at)
                .limit(batch)
            )
            return [
                _Candidate(
                    id=intent.id,
                    customer_id=intent.customer_id,
                    tenant_type=intent.tenant_type,
                    tenant_id=intent.tenant_id,
                    operation=intent.operation,
                    status=intent.status,
                    external_customer=external,
                    created_at=intent.created_at,
                    session_id=intent.psp_session_id,
                    session_expires_at=intent.psp_session_expires_at,
                )
                for intent, external in rows.all()
            ]

    @classmethod
    async def _stale_pending(
        cls, cutoff: datetime, max_attempts: int, batch: int
    ) -> list[_Candidate]:
        return await cls._select(
            OperationIntent.status == INTENT_PENDING,
            OperationIntent.created_at < cutoff,
            OperationIntent.reconcile_attempts < max_attempts,
            batch=batch,
        )

    @classmethod
    async def _owed_expiries(cls, max_attempts: int, batch: int) -> list[_Candidate]:
        # A request-path expire that FAILED records an attempt (> 0); one that
        # succeeded sets psp_expired_at. Revoked rows from before the outcome
        # was recorded have 0 attempts and are not selected.
        return await cls._select(
            OperationIntent.status == INTENT_REVOKED,
            OperationIntent.operation == OPERATION_CHECKOUT,
            OperationIntent.psp_session_id.is_not(None),
            OperationIntent.psp_expired_at.is_(None),
            OperationIntent.reconcile_attempts > 0,
            OperationIntent.reconcile_attempts < max_attempts,
            batch=batch,
        )

    # --- (a) stale pending ------------------------------------------------------

    @classmethod
    async def _reconcile_pending(  # noqa: C901
        cls,
        psp: StripeClientPort,
        c: _Candidate,
        now: datetime,
        max_attempts: int,
        report: ReconcileReport,
    ) -> None:
        if c.operation != OPERATION_CHECKOUT:
            # A portal session cannot be listed or expired at the PSP; its URL
            # was never handed out (and is never stored).
            if await cls._close(
                c, INTENT_REVOKED, "reconciled: stale pending portal intent"
            ):
                report.reconciled += 1
            return
        if not c.external_customer:
            # The PSP customer is linked before any session call: no customer,
            # no session.
            if await cls._close(c, INTENT_FAILED, "reconciled: no PSP customer"):
                report.reconciled += 1
            return
        try:
            sessions = await cls._sessions_of(psp, c)
        except Exception as exc:  # noqa: BLE001 - bounded retry, then operator
            await cls._failed_attempt(c, exc, now, max_attempts, report)
            return
        if not sessions:
            if await cls._close(c, INTENT_FAILED, "reconciled: no PSP session found"):
                report.reconciled += 1
            return

        open_sessions = [s for s in sessions if s.status == CHECKOUT_OPEN]
        paid = any(s.status == CHECKOUT_COMPLETE for s in sessions)
        primary = (open_sessions or sessions)[0]
        extra: dict[str, Any] = {}
        if paid:
            extra["followup_at"] = now
        if not open_sessions and not paid:
            extra["psp_expired_at"] = now  # every session already expired
        closed = await close_pending_intent(
            intent_id=c.id,
            customer_id=c.customer_id,
            tenant_type=c.tenant_type,
            tenant_id=c.tenant_id,
            outcome=INTENT_REVOKED,
            reason=(
                "reconciled: stale pending intent; checkout session "
                + ("completed without an issued URL" if paid else "not issued")
            ),
            session_id=primary.id,
            extra=extra,
        )
        if not closed:
            return  # no longer pending (e.g. issued meanwhile): never touched
        report.reconciled += 1
        if paid:
            logger.error(
                "billing intent %s: its checkout session was completed while the "
                "intent was pending; revoked, operator follow-up (refund is manual)",
                c.id,
            )
        for session in open_sessions:
            await cls._expire(psp, c, session.id, max_attempts, now, report)

    # --- (b) owed compensation --------------------------------------------------

    @classmethod
    async def _retry_expire(
        cls,
        psp: StripeClientPort,
        c: _Candidate,
        now: datetime,
        max_attempts: int,
        report: ReconcileReport,
    ) -> None:
        expires_at = c.session_expires_at or c.created_at + PSP_DEFAULT_SESSION_LIFETIME
        if expires_at <= now:
            # The PSP has expired it by itself: nothing is owed any more.
            await cls._mark_expired(c, expires_at)
            report.compensated += 1
            return
        try:
            sessions = await cls._sessions_of(psp, c)
        except Exception as exc:  # noqa: BLE001 - bounded retry, then operator
            await cls._failed_attempt(c, exc, now, max_attempts, report)
            return
        session = next((s for s in sessions if s.id == c.session_id), None)
        if session is not None and session.status == CHECKOUT_EXPIRED:
            await cls._mark_expired(c, now)
            report.compensated += 1
            return
        if session is not None and session.status == CHECKOUT_COMPLETE:
            # Paid after the refusal: nothing to expire; the webhook gate
            # refused it — an operator refunds. Stop retrying.
            await cls._escalate(
                c, "checkout session completed after revocation", now, max_attempts
            )
            report.escalated += 1
            return
        assert c.session_id is not None  # noqa: S101 - type narrowing of an invariant
        await cls._expire(psp, c, c.session_id, max_attempts, now, report)

    # --- shared steps -------------------------------------------------------------

    @staticmethod
    async def _sessions_of(
        psp: StripeClientPort, c: _Candidate
    ) -> list[CheckoutSessionInfo]:
        """The intent's checkout sessions: listed for the intent's OWN PSP
        customer, matched on the intent marker."""
        assert c.external_customer is not None  # noqa: S101 - type narrowing of an invariant
        listed = await psp.list_checkout_sessions(
            customer_id=c.external_customer,
            created_gte=int((c.created_at - _LOOKUP_SKEW).timestamp()),
        )
        return [s for s in listed if s.metadata.get(INTENT_METADATA_KEY) == str(c.id)]

    @classmethod
    async def _expire(
        cls,
        psp: StripeClientPort,
        c: _Candidate,
        session_id: str,
        max_attempts: int,
        now: datetime,
        report: ReconcileReport,
    ) -> None:
        try:
            await psp.expire_checkout_session(session_id)
        except Exception as exc:  # noqa: BLE001 - bounded retry, then operator
            await cls._failed_attempt(
                _revoked(c, session_id), exc, now, max_attempts, report
            )
            return
        await record_expire_outcome(c.id, c.customer_id, error=None)
        if c.status == INTENT_REVOKED:
            report.compensated += 1

    @staticmethod
    async def _close(c: _Candidate, outcome: str, reason: str) -> bool:
        return await close_pending_intent(
            intent_id=c.id,
            customer_id=c.customer_id,
            tenant_type=c.tenant_type,
            tenant_id=c.tenant_id,
            outcome=outcome,
            reason=reason,
        )

    @staticmethod
    async def _mark_expired(c: _Candidate, at: datetime) -> None:
        async with open_uow() as db:
            await db.execute(
                update(OperationIntent)
                .where(*_owner_and_status(c))
                .values(psp_expired_at=at, reconcile_error=None)
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    async def _escalate(
        c: _Candidate, error: str, now: datetime, max_attempts: int
    ) -> None:
        async with open_uow() as db:
            await db.execute(
                update(OperationIntent)
                .where(*_owner_and_status(c))
                .values(
                    reconcile_error=error,
                    followup_at=func.coalesce(OperationIntent.followup_at, now),
                    # Exhausted: never selected again.
                    reconcile_attempts=func.greatest(
                        OperationIntent.reconcile_attempts, max_attempts
                    ),
                )
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    async def _failed_attempt(
        c: _Candidate,
        exc: BaseException,
        now: datetime,
        max_attempts: int,
        report: ReconcileReport,
    ) -> None:
        """One more failed attempt (+ the error); at the limit, ``followup_at``."""
        error = f"{type(exc).__name__}: {exc}"[:500]
        attempts = OperationIntent.reconcile_attempts + 1
        async with open_uow() as db:
            result = await db.execute(
                update(OperationIntent)
                .where(*_owner_and_status(c))
                .values(
                    reconcile_attempts=attempts,
                    reconcile_error=error,
                    followup_at=case(
                        (
                            and_(
                                attempts >= max_attempts,
                                OperationIntent.followup_at.is_(None),
                            ),
                            now,
                        ),
                        else_=OperationIntent.followup_at,
                    ),
                )
                .returning(OperationIntent.reconcile_attempts)
                .execution_options(synchronize_session=False)
            )
            recorded = result.scalar_one_or_none()
        if recorded is None:
            return  # moved or expired meanwhile: nothing failed for it
        if recorded >= max_attempts:
            report.escalated += 1
            logger.error(
                "billing intent %s (%s): reconciliation failed %d times (%s); "
                "left to an operator (followup_at)",
                c.id,
                c.status,
                recorded,
                type(exc).__name__,
            )
        else:
            report.still_failing += 1
            logger.warning(
                "billing intent %s (%s): reconciliation step failed (%s); retried "
                "by the next run",
                c.id,
                c.status,
                type(exc).__name__,
            )


def _revoked(c: _Candidate, session_id: str) -> _Candidate:
    """The candidate after this run revoked it (its expire is now owed)."""
    return _Candidate(
        id=c.id,
        customer_id=c.customer_id,
        tenant_type=c.tenant_type,
        tenant_id=c.tenant_id,
        operation=c.operation,
        status=INTENT_REVOKED,
        external_customer=c.external_customer,
        created_at=c.created_at,
        session_id=session_id,
        session_expires_at=c.session_expires_at,
    )


def _owner_and_status(c: _Candidate) -> tuple[Any, ...]:
    """Every reconciler write: this intent, of its own customer, still in the
    status it was selected in (an intent that moved is never written), and
    whose session is not known to be expired yet — a concurrent successful
    expire (another run, the request path) is never overwritten by, or
    counted as, this run's failure."""
    return (
        OperationIntent.id == c.id,
        OperationIntent.customer_id == c.customer_id,
        OperationIntent.status == c.status,
        OperationIntent.psp_expired_at.is_(None),
    )


__all__ = ["DEFAULT_BATCH", "IntentReconciler", "ReconcileReport"]
