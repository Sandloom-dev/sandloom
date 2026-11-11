"""Billing's durable evidence of reconciler progress and outstanding compensation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.data import WallClock, conflict_insert, open_uow
from sandloom.sdk.module import HealthResult
from sandloom_billing.models import (
    INTENT_PENDING,
    INTENT_REVOKED,
    BillingReconcilerRun,
    OperationIntent,
)

RECONCILE_INTERVAL_SECONDS = 300.0
RECONCILE_MAX_SILENCE_SECONDS = 3 * RECONCILE_INTERVAL_SECONDS
RECONCILER_JOB = "reconcile_operation_intents"
RECONCILER_HEALTH_CHECK = "billing_reconciler"

RunRecord = tuple[datetime, int]


async def record_reconciler_run(db: AsyncSession) -> None:
    """Record a completed run using the database clock."""
    stmt = conflict_insert(BillingReconcilerRun, session=db).values(
        name=RECONCILER_JOB, finished_at=WallClock(), runs=1
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[BillingReconcilerRun.name],
            set_={
                "finished_at": WallClock(),
                "runs": BillingReconcilerRun.runs + 1,
            },
        )
    )


async def _run_record(db: AsyncSession) -> RunRecord | None:
    row = (
        await db.execute(
            select(BillingReconcilerRun.finished_at, BillingReconcilerRun.runs).where(
                BillingReconcilerRun.name == RECONCILER_JOB
            )
        )
    ).one_or_none()
    if row is None:
        return None
    finished_at, runs = row
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    return finished_at, int(runs)


async def last_reconciler_run() -> RunRecord | None:
    async with open_uow() as db:
        return await _run_record(db)


def _age(last: RunRecord, now: datetime) -> float:
    return (now - last[0]).total_seconds()


async def require_reconciler_progress(db: AsyncSession) -> None:
    """Refuse an intent that may need background compensation before a PSP call."""
    last = await _run_record(db)
    if (
        last is None
        or not 0 <= _age(last, datetime.now(UTC)) <= RECONCILE_MAX_SILENCE_SECONDS
    ):
        raise HTTPException(
            status_code=503,
            detail="Billing checkout and portal are temporarily unavailable "
            "while reconciliation is stopped or unconfirmed.",
        )


async def _compensations() -> tuple[int, int]:
    async with open_uow() as db:
        failed, followups = (
            await db.execute(
                select(
                    func.count().filter(
                        OperationIntent.reconcile_error.is_not(None),
                        OperationIntent.followup_at.is_(None),
                    ),
                    func.count().filter(OperationIntent.followup_at.is_not(None)),
                ).where(
                    OperationIntent.status.in_((INTENT_PENDING, INTENT_REVOKED)),
                    OperationIntent.psp_expired_at.is_(None),
                )
            )
        ).one()
    return int(failed), int(followups)


class ReconcilerHealthCheck:
    """Report durable progress and open compensation failures on /health."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        read: Callable[[], Awaitable[RunRecord | None]] = last_reconciler_run,
        compensations: Callable[[], Awaitable[tuple[int, int]]] = _compensations,
    ) -> None:
        self._now = now
        self._read = read
        self._compensations = compensations

    async def __call__(self) -> HealthResult:
        last = await self._read()
        failed, followups = await self._compensations()
        details: dict[str, object] = {
            "job": RECONCILER_JOB,
            "max_silence_seconds": RECONCILE_MAX_SILENCE_SECONDS,
            "failed_compensations": failed,
            "operator_followups": followups,
        }
        if last is None:
            details.update(
                state="never_ran",
                error="no completed reconciler run: check the worker and beat",
            )
            return HealthResult(status="fail", details=details)

        age = _age(last, self._now())
        details.update(
            last_run_at=last[0].isoformat(),
            seconds_since_last_run=round(age, 1),
            runs=last[1],
        )
        if not 0 <= age <= RECONCILE_MAX_SILENCE_SECONDS:
            details.update(
                state="overdue" if age >= 0 else "clock_skew",
                error=(
                    "no recent reconciler run: check worker, beat, broker "
                    "and task errors."
                ),
            )
            return HealthResult(status="fail", details=details)

        details["state"] = "running"
        if failed or followups:
            details["error"] = "billing intents need compensation or operator follow-up"
            return HealthResult(status="fail", details=details)
        return HealthResult(status="ok", details=details)
