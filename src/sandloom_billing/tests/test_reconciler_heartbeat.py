"""Durable Billing operational health and compensation-dependent HTTP writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingReconcilerRun, OperationIntent
from sandloom_billing.services import intent_reconciler
from sandloom_billing.services.reconciler_health import (
    RECONCILE_INTERVAL_SECONDS,
    RECONCILE_MAX_SILENCE_SECONDS,
    RECONCILER_HEALTH_CHECK,
    ReconcilerHealthCheck,
    last_reconciler_run,
    record_reconciler_run,
)

_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _check(
    last: tuple[datetime, int] | None, *, failures: tuple[int, int] = (0, 0)
) -> ReconcilerHealthCheck:
    async def read() -> tuple[datetime, int] | None:
        return last

    async def compensations() -> tuple[int, int]:
        return failures

    return ReconcilerHealthCheck(
        now=lambda: _NOW, read=read, compensations=compensations
    )


def test_the_allowed_interval_is_three_periods() -> None:
    assert RECONCILE_MAX_SILENCE_SECONDS == 3 * RECONCILE_INTERVAL_SECONDS == 900.0


async def test_restarting_the_web_process_never_grants_startup_grace() -> None:
    for _ in range(2):
        result = await _check(None)()
        assert result.status == "fail"
        assert result.details["state"] == "never_ran"
        assert "worker and beat" in result.details["error"]


async def test_fresh_progress_and_failed_compensations_are_reported_separately() -> (
    None
):
    result = await _check((_NOW, 1), failures=(2, 1))()
    assert result.status == "fail"
    assert result.details["state"] == "running"
    assert result.details["failed_compensations"] == 2
    assert result.details["operator_followups"] == 1


@pytest.mark.parametrize(
    ("age", "status", "state"),
    [
        (RECONCILE_INTERVAL_SECONDS, "ok", "running"),
        (RECONCILE_MAX_SILENCE_SECONDS, "ok", "running"),
        (RECONCILE_MAX_SILENCE_SECONDS + 1, "fail", "overdue"),
    ],
)
async def test_a_run_older_than_the_allowed_interval_fails_as_overdue(
    age: float, status: str, state: str
) -> None:
    check = _check((_NOW - timedelta(seconds=age), 7))

    result = await check()

    assert result.status == status
    assert result.details["state"] == state
    assert result.details["seconds_since_last_run"] == age
    assert result.details["runs"] == 7
    assert result.details["max_silence_seconds"] == RECONCILE_MAX_SILENCE_SECONDS


async def test_each_completed_run_records_itself(
    async_client: AsyncClient, fake_psp: Any
) -> None:
    """The task's run (what beat sends a worker) records its end in its last
    unit, with the database's clock; the next run moves it on."""
    from sandloom_billing import tasks

    assert await last_reconciler_run() is None

    await tasks.reconcile_intents_once(fake_psp)
    first = await last_reconciler_run()
    await tasks.reconcile_intents_once(fake_psp)
    second = await last_reconciler_run()

    assert first is not None
    assert second is not None
    assert (first[1], second[1]) == (1, 2)
    assert second[0] >= first[0]
    assert abs((datetime.now(UTC) - second[0]).total_seconds()) < 60


async def test_a_run_that_raises_records_nothing(
    async_client: AsyncClient, fake_psp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconciler that keeps failing shows as one that does not run."""
    from sandloom_billing import tasks

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database gone")

    monkeypatch.setattr(intent_reconciler.IntentReconciler, "run", broken)

    with pytest.raises(RuntimeError):
        await tasks.reconcile_intents_once(fake_psp)

    assert await last_reconciler_run() is None


async def test_the_health_check_reads_the_recorded_run(
    async_client: AsyncClient, fake_psp: Any
) -> None:
    """End to end on the database: a recorded run is ``running``."""
    from sandloom_billing import tasks

    check = ReconcilerHealthCheck()
    assert (await check()).details["state"] == "never_ran"

    await tasks.reconcile_intents_once(fake_psp)
    result = await check()

    assert result.status == "ok", result.details
    assert result.details["state"] == "running"
    assert result.details["runs"] == 1


def test_billing_registers_the_reconciler_check(sandloom_core: Any) -> None:
    """Billing contributes its check to operational health."""
    check = sandloom_core.health_checks.get(RECONCILER_HEALTH_CHECK)

    assert isinstance(check, ReconcilerHealthCheck)


@pytest.mark.parametrize("outage", ["missing", "overdue"])
async def test_new_psp_effects_wait_for_reconciler_recovery(
    outage: str,
    async_client: AsyncClient,
    test_db: AsyncSession,
    user_a: tuple[Any, dict[str, str]],
    catalog: dict[str, Any],
    fake_psp: Any,
) -> None:
    if outage == "missing":
        await test_db.execute(delete(BillingReconcilerRun))
    else:
        await test_db.execute(
            update(BillingReconcilerRun).values(
                finished_at=datetime.now(UTC) - timedelta(seconds=901)
            )
        )
    await test_db.commit()
    _, headers = user_a
    assert (await async_client.get("/health/simple")).status_code == 200
    assert (await async_client.get("/api/v1/billing/plans")).status_code == 200

    routes = (
        ("checkout-session", {"price_id": catalog["pro_month"].id}),
        ("customer-portal", {}),
    )
    for route, body in routes:
        response = await async_client.post(
            f"/api/v1/billing/{route}",
            headers={**headers, "Idempotency-Key": f"stopped-{route}"},
            json=body,
        )
        assert response.status_code == 503, response.text
    assert not fake_psp.created_customers
    assert not fake_psp.checkout_sessions

    await record_reconciler_run(test_db)
    await test_db.commit()
    for route, body in routes:
        response = await async_client.post(
            f"/api/v1/billing/{route}",
            headers={**headers, "Idempotency-Key": f"recovered-{route}"},
            json=body,
        )
        assert response.status_code == 200, response.text
    assert len(fake_psp.checkout_sessions) == 1

    # A real outstanding record fails operational health even with a fresh run.
    await test_db.execute(
        update(OperationIntent)
        .where(OperationIntent.operation == "checkout")
        .values(status="revoked", reconcile_attempts=1, reconcile_error="test")
    )
    await test_db.commit()
    check = await ReconcilerHealthCheck()()
    assert check.status == "fail"
    assert check.details["state"] == "running"
    assert check.details["failed_compensations"] == 1
