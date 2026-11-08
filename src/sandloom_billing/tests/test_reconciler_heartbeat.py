"""The intent reconciler's operational criterion (KB-IC-3; owner decision on
infrastructure dependencies, answer 3).

``broker ok`` is not enough: the broker can answer while no worker or no beat
runs the reconciler. The criterion is the reconciler's own record:

- every completed run records its end (``billing_reconciler_runs``); a run
  that raises records nothing;
- the ``billing_reconciler`` health check (``/health`` and ``/ready`` of every
  process that loads billing) fails when no run completed within
  ``RECONCILE_MAX_SILENCE_SECONDS`` (three periods): ``never_ran`` (no worker
  or no beat) or ``overdue`` (they, or the broker, stopped, or every run
  fails);
- a process gives the reconciler one allowed interval after it starts, and
  reads nothing in that time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from sandloom_billing.services import intent_reconciler
from sandloom_billing.services.intent_reconciler import (
    RECONCILE_INTERVAL_SECONDS,
    RECONCILE_MAX_SILENCE_SECONDS,
    RECONCILER_HEALTH_CHECK,
    ReconcilerHealthCheck,
    last_reconciler_run,
)

_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class _Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


def _check(
    clock: _Clock, last: tuple[datetime, int] | None, reads: list[int]
) -> ReconcilerHealthCheck:
    async def read() -> tuple[datetime, int] | None:
        reads.append(1)
        return last

    return ReconcilerHealthCheck(monotonic=clock, now=lambda: _NOW, read=read)


def test_the_allowed_interval_is_three_periods() -> None:
    assert RECONCILE_MAX_SILENCE_SECONDS == 3 * RECONCILE_INTERVAL_SECONDS == 900.0


async def test_a_process_waits_one_interval_before_judging_and_reads_nothing() -> None:
    clock, reads = _Clock(), []
    check = _check(clock, None, reads)

    clock.at += RECONCILE_MAX_SILENCE_SECONDS - 1
    result = await check()

    assert result.status == "ok"
    assert result.details["state"] == "startup_grace"
    assert reads == []


async def test_no_run_ever_recorded_fails_as_never_ran() -> None:
    clock, reads = _Clock(), []
    check = _check(clock, None, reads)

    clock.at += RECONCILE_MAX_SILENCE_SECONDS + 1
    result = await check()

    assert result.status == "fail"
    assert result.details["state"] == "never_ran"
    assert "no worker or no beat" in result.details["error"]
    assert reads == [1]


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
    clock, reads = _Clock(), []
    check = _check(clock, (_NOW - timedelta(seconds=age), 7), reads)
    clock.at += RECONCILE_MAX_SILENCE_SECONDS + 1

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

    clock = _Clock()
    check = ReconcilerHealthCheck(monotonic=clock)
    clock.at += RECONCILE_MAX_SILENCE_SECONDS + 1
    assert (await check()).details["state"] == "never_ran"

    await tasks.reconcile_intents_once(fake_psp)
    result = await check()

    assert result.status == "ok", result.details
    assert result.details["state"] == "running"
    assert result.details["runs"] == 1


def test_billing_registers_the_reconciler_check(sandloom_core: Any) -> None:
    """Every process that loads billing carries the check in ``/health`` and
    ``/ready`` (the kernel names no module: billing registers it)."""
    check = sandloom_core.health_checks.get(RECONCILER_HEALTH_CHECK)

    assert isinstance(check, ReconcilerHealthCheck)
