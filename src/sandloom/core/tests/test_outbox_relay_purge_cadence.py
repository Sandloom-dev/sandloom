"""Relay housekeeping keeps up with the write rate (R3).

The retention purge used to run every 60 relay TICKS with at most 10 batches:
an idle relay ticks every ~5 s, so it deleted at most ~10k rows per 5 minutes
(~30 rows/s) and the outbox grew without bound above that rate. The purge now
runs on a wall-clock cadence and keeps batching while batches come back full,
bounded by a time budget.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.infra.contracts import RelayBatchResult
from sandloom.core.tasks import relay


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(relay, "_monotonic", c, raising=False)
    return c


async def test_purge_runs_on_a_time_cadence_not_a_tick_count(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay.broker_circuit().reset()
    monkeypatch.setattr(relay, "_PURGE_SCHEDULE", relay._PurgeSchedule(), raising=False)
    monkeypatch.setattr(relay, "_TICKS", relay._TickCounter())
    purges: list[float] = []
    monkeypatch.setattr(
        relay, "_database_targets", lambda _core: [("default", object(), object())]
    )

    async def idle_batch(*_a: Any, **_kw: Any) -> RelayBatchResult:
        return RelayBatchResult()

    async def purge(*_a: Any, **_kw: Any) -> int:
        purges.append(clock.now)
        return 0

    monkeypatch.setattr(relay, "_relay_batch_for_store", idle_batch)
    monkeypatch.setattr(relay, "purge_delivered", purge)
    monkeypatch.setattr(relay, "purge_expired_commands", purge)
    core = SimpleNamespace(event_bus=None)
    # An idle relay ticks every ~5 s; over two minutes of idling the purge must
    # run every interval (30 s), not once per 60 ticks (= every 5 minutes).
    for _ in range(25):
        await relay._relay_tick(core)  # type: ignore[arg-type]
        clock.now += 5.0
    interval = relay._PURGE_INTERVAL_SECONDS
    assert 0 < interval <= 60
    # Two purge helpers per run (delivered outbox + expired commands).
    runs = len(purges) // 2
    assert runs >= int(120 // interval)


class _FullStore:
    """An outbox store whose backlog spans ``backlog`` full batches."""

    def __init__(self, backlog: int, clock: _Clock, cost: float = 0.0) -> None:
        self.backlog = backlog
        self.calls = 0
        self.clock = clock
        self.cost = cost

    async def purge_delivered(
        self, _factory: Any, *, older_than_seconds: float, limit: int
    ) -> int:
        self.calls += 1
        self.clock.now += self.cost
        if self.backlog <= 0:
            return 0
        self.backlog -= 1
        return limit


async def test_purge_keeps_batching_while_batches_are_full(clock: _Clock) -> None:
    store = _FullStore(backlog=25, clock=clock)
    deleted = await relay.purge_delivered(
        object(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        retention_hours=1,
        batch_size=100,
    )
    assert deleted == 25 * 100


async def test_purge_stops_at_its_time_budget(clock: _Clock) -> None:
    budget = relay._PURGE_TIME_BUDGET_SECONDS
    store = _FullStore(backlog=10_000, clock=clock, cost=budget / 4)
    deleted = await relay.purge_delivered(
        object(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        retention_hours=1,
        batch_size=100,
    )
    assert store.calls == 4
    assert deleted == 4 * 100


async def test_relay_health_distinguishes_idle_stalled_and_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "outbox_relay_stall_seconds", 60.0)

    async def waiting() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(waiting())
    relay._RELAY_EXPECTED[task] = True
    relay._RELAY_PROGRESS[task] = time.monotonic()
    try:
        status = relay.relay_health_status(task)
        assert status["enabled"] and status["running"] and not status["stalled"]
        relay._RELAY_PROGRESS[task] -= 120
        assert relay.relay_health_status(task)["stalled"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not relay.relay_health_status(task)["running"]
