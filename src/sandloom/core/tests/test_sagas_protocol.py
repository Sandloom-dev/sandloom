"""The saga step protocol end to end (ADR-0008 §6), through ``saga_harness``.

One test per row of the §6 failure table, plus the happy path and the row
lock. Every command call goes through the real durable command log (the
providers are harness fakes registered as command contracts).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import func, select

import sandloom.core.sql_alchemy.saga_store as store
from sandloom.core.module_api import ContractCallInProgress
from sandloom.core.sagas import runtime as saga_runtime
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import (
    Ledger,
    enroll_input,
    enroll_saga,
    install_fakes,
)
from sandloom.core.unit_of_work import db_session

ENROLL = enroll_saga()


def _ledger(saga_harness: Any) -> Ledger:
    ledger = Ledger()
    install_fakes(saga_harness, ledger)
    return ledger


async def test_happy_path_runs_every_step_once_with_derived_keys(saga_harness) -> None:
    ledger = _ledger(saga_harness)

    run = await saga_harness.run(ENROLL, input=enroll_input(price=30))

    assert run.status == "completed"
    assert run.calls == ["charge:do", "grant_access:do", "notify:do"]
    run.assert_keys_derived()
    assert ledger.balance == 70
    assert len(ledger.mails) == 1
    assert run.results["charge"] == 70
    assert run.results["grant_access"]["balance"] == 70
    assert run.step_row("charge")["status"] == "succeeded"
    assert run.step_row("charge")["operation_key"] == (
        f"saga:{ENROLL.saga_type}:{run.saga_id}:charge:do"
    )
    # every transition published its next nudge through the outbox
    assert run.transitions == ["advanced", "advanced", "advanced"]


async def test_start_publishes_the_first_nudge_in_the_callers_transaction(
    saga_harness, db_engine: Any
) -> None:
    _ledger(saga_harness)
    handle = await saga_harness.start(ENROLL, saga_id="s-1", input=enroll_input())
    # saga_type is known once the harness registered ENROLL (by start above).
    channel = f"{ENROLL.saga_type.split('.')[0]}.saga_advance.v1"
    assert handle.created and handle.status == "running" and handle.step == "charge"
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.channel == channel)
            )
        ).scalar_one()
    assert rows == 1


async def test_crash_before_the_call_rolls_back_and_the_redrive_runs_the_step(
    saga_harness,
) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.crash_before("charge")

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.crashes == 1
    assert run.status == "completed"
    assert len(saga_harness.fake_calls[f"{'sgbilling.credits.v1'}.spend"]) == 1
    assert ledger.balance == 70


async def test_crash_after_the_provider_committed_replays_the_same_key(
    saga_harness,
) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.crash_after("charge")

    run = await saga_harness.run(ENROLL, input=enroll_input(price=30))

    assert run.crashes == 1
    assert run.status == "completed"
    # re-called with the SAME key: the command log replayed the first result
    assert run.calls[:2] == ["charge:do", "charge:do"]
    assert run.keys[0] == run.keys[1]
    assert len(saga_harness.fake_calls["sgbilling.credits.v1.spend"]) == 1
    assert ledger.balance == 70
    assert run.results["charge"] == 70


async def test_committed_transition_redelivered_is_a_no_op(saga_harness) -> None:
    """Ambiguous COMMIT, committed side: the old nudge comes again."""
    _ledger(saga_harness)
    handle = await saga_harness.start(ENROLL, saga_id="amb-1", input=enroll_input())
    reg = saga_harness.register(ENROLL)
    first = await saga_harness.instance(reg, handle.saga_id)

    assert await saga_harness.deliver(reg, first, 0) == saga_runtime.ADVANCED
    # the COMMIT succeeded but the delivery is retried: stale version
    assert await saga_harness.deliver(reg, first, 0) == saga_runtime.NOOP_STALE

    run = await saga_harness.drive(ENROLL, handle.saga_id)
    assert run.status == "completed"
    assert run.calls == ["grant_access:do", "notify:do"]


async def test_duplicate_deliveries_never_run_a_step_twice(saga_harness) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.deliver_twice()

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "completed"
    assert run.calls == ["charge:do", "grant_access:do", "notify:do"]
    assert saga_runtime.NOOP_STALE in run.transitions
    assert ledger.balance == 70


async def test_provider_in_flight_is_transient_and_retried(saga_harness) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("charge", exception=ContractCallInProgress("busy"))

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "completed"
    assert run.calls[:2] == ["charge:do", "charge:do"]
    assert saga_runtime.RETRY_SCHEDULED in run.transitions
    assert run.step_row("charge")["attempts"] == 2
    assert ledger.balance == 70


async def test_a_locked_instance_is_skipped_not_waited_for(saga_harness) -> None:
    """``FOR UPDATE SKIP LOCKED``: another worker owns the transition."""
    _ledger(saga_harness)
    handle = await saga_harness.start(ENROLL, saga_id="lock-1", input=enroll_input())
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with db_session() as session:
            await store.lock_instance(
                session, handle.instance_id, tenant=store.ANY_TENANT, skip_locked=False
            )
            holding.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    try:
        async with db_session() as session:
            result = await saga_harness.runtime.advance(
                session, handle.instance_id, 0, tenant=None
            )
        assert result == saga_runtime.NOOP_LOCKED
    finally:
        release.set()
        await holder


async def test_unknown_definition_parks_instead_of_crashing(
    saga_harness, db_engine: Any
) -> None:
    _ledger(saga_harness)
    handle = await saga_harness.start(ENROLL, saga_id="unk-1", input=enroll_input())
    saga_harness.runtime.registry = saga_runtime.SagaRegistry([])
    async with db_session() as session:
        result = await saga_harness.runtime.advance(
            session, handle.instance_id, 0, tenant=None
        )
    assert result == saga_runtime.PARKED
    reg = saga_harness.register(ENROLL)
    row = await saga_harness.instance(reg, handle.saga_id)
    assert row.status == "parked" and row.park_reason == "unknown_definition"


async def test_malformed_nudge_is_poison(saga_harness) -> None:
    import pytest

    from sandloom.core.eventing.delivery import PoisonEventError

    with pytest.raises(PoisonEventError):
        saga_runtime.parse_nudge({"id": "x", "payload": {"instance_id": "nope"}})
    instance_id, version, tenant = saga_runtime.parse_nudge(
        {"payload": {"instance_id": str(uuid.UUID(int=3)), "expected_version": 2}}
    )
    assert (instance_id.int, version, tenant) == (3, 2, None)
