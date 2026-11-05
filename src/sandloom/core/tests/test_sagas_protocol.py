"""The saga step protocol end to end (ADR-0008 §6), through ``saga_harness``.

One test per row of the §6 failure table, plus the happy path and the row
lock. Every command call goes through the real durable command log (the
providers are harness fakes registered as command contracts).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

import sandloom.core.sql_alchemy.saga_store as store
from sandloom.core.module_api import ContractCallInProgress
from sandloom.core.sagas import runtime as saga_runtime
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.testing.infra import has_serial_writers, skip_unless_db_feature
from sandloom.core.tests.saga_fixtures import (
    DurableLedger,
    Ledger,
    command_records,
    committed_effects,
    create_effect_tables,
    durable_balance,
    enroll_input,
    enroll_saga,
    install_fakes,
    step_status,
)
from sandloom.core.unit_of_work import db_session

ENROLL = enroll_saga()
#: How long a racer waits for the other (seconds): a lost race fails, never hangs.
RACE_WAIT = 30.0


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


CRASH_POINTS = ("before_the_call", "after_the_effect", "after_the_commit")


def _one_delivery() -> Any:
    """``stop_when`` that lets ``drive`` make exactly one delivery."""
    seen: list[int] = []

    def stop(row: Any) -> bool:
        seen.append(row.version)
        return len(seen) > 1

    return stop


@pytest.mark.parametrize("crash_point", CRASH_POINTS)
async def test_a_crash_at_any_point_of_a_step_converges_without_reapplying_it(
    saga_harness, db_engine: Any, crash_point: str
) -> None:
    """G-SAGA-07 and G-CMD-01 as one law per crash point, on every database.

    The provider's effect (a row, :class:`DurableLedger`), its dedupe record
    and the step's state are read as committed right after the crash, then
    the saga is driven to the end: the step's effect is applied exactly once.

    After the effect the two databases differ in HOW, never in the outcome,
    and the branch follows the database's fact (``serial_writers``), not its
    name. Where the provider commits on its own (PostgreSQL), the effect and
    the record survive the crash and the redrive replays the same key from
    the command log (the body never runs again). On a serial-writer database
    the provider's unit joins the transition's transaction (G-UOW-09): a
    provider commit independent of the caller cannot happen there, so instead
    the crash loses the effect, the record and the step's state together and
    the redrive runs the step once.

    A crash INSIDE the provider (research §2's list) is no point of its own:
    on a serial-writer database the provider's work rolls back with the
    transition, as ``after_the_effect`` does there; on PostgreSQL a provider
    that dies before its own COMMIT leaves nothing but the marker, as
    ``before_the_call`` does (G-CMD-04 covers the killed provider itself).
    For a provider elsewhere (RPC, another instance) see
    ``test_sagas_recovery.py``'s ``elsewhere`` tests."""
    await create_effect_tables(db_engine)
    serial = has_serial_writers(db_engine)
    ledger = DurableLedger()
    install_fakes(saga_harness, ledger)
    spend = "sgbilling.credits.v1.spend"
    if crash_point == "before_the_call":
        saga_harness.crash_before("charge")
    elif crash_point == "after_the_effect":
        saga_harness.crash_after("charge")
    reg = saga_harness.register(ENROLL)
    handle = await saga_harness.start(
        ENROLL, saga_id=uuid.uuid4(), input=enroll_input(price=30)
    )
    key = f"saga:{ENROLL.saga_type}:{handle.saga_id}:charge:do"
    before = await saga_harness.instance(reg, handle.saga_id)

    first = await saga_harness.drive(ENROLL, handle.saga_id, stop_when=_one_delivery())
    if crash_point == "after_the_commit":
        # the transition committed, then the process died before the nudge
        # was acknowledged: the same nudge comes again
        assert first.crashes == 0
        redelivered = await saga_harness.deliver(reg, before, before.version)
        assert redelivered == saga_runtime.NOOP_STALE
    else:
        assert first.crashes == 1
    at_crash = (
        await committed_effects(db_engine, key),
        await command_records(db_engine, spend, key),
        await step_status(db_engine, key),
    )
    if crash_point == "before_the_call":
        # nothing ran; the marker commits in its own unit where units are
        # independent, and with the (lost) transition on a serial writer
        assert at_crash == ([], 0, None if serial else "started")
    elif crash_point == "after_the_commit":
        assert at_crash == ([30], 1, "succeeded")
    elif serial:
        # all or nothing: the effect, its record and the step's state are lost
        assert at_crash == ([], 0, None)
    else:
        # the provider committed on its own; the step's state did not
        assert at_crash == ([30], 1, "started")

    rest = await saga_harness.drive(ENROLL, handle.saga_id)

    assert rest.status == "completed"
    assert await committed_effects(db_engine, key) == [30]
    assert await command_records(db_engine, spend, key) == 1
    assert await step_status(db_engine, key) == "succeeded"
    assert await durable_balance(db_engine, handle.saga_id) == 70
    assert rest.results["charge"] == 70
    # a provider on the transition's own instance never needs a marked one
    assert saga_runtime.MARKED not in first.transitions + rest.transitions
    run_calls, run_keys = first.calls + rest.calls, first.keys + rest.keys
    executions = len(saga_harness.fake_calls[spend])
    if crash_point == "after_the_commit":
        assert run_calls == ["charge:do", "grant_access:do", "notify:do"]
        assert executions == 1
    elif crash_point == "before_the_call":
        assert run_calls[:2] == ["charge:do", "charge:do"]
        assert executions == 1
        assert ledger.balance == 70
    elif not serial:
        # re-called with the SAME key: the command log replayed the first result
        assert first.crashes + rest.crashes == 1
        assert run_calls[:2] == ["charge:do", "charge:do"]
        assert run_keys[0] == run_keys[1]
        assert executions == 1
        assert ledger.balance == 70
    else:
        # re-called with the same key: the first execution left nothing, so
        # the body ran again and only this run committed (the in-memory
        # ledger is not transactional and still holds the lost first run)
        assert first.crashes + rest.crashes == 1
        assert run_calls[:2] == ["charge:do", "charge:do"]
        assert run_keys[0] == run_keys[1]
        assert executions == 2


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


# The claim's own FOR UPDATE SKIP LOCKED while another worker holds the row:
# a database without skip_locked (SQLite) claims by compare-and-set instead.
@skip_unless_db_feature("skip_locked")
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
            await asyncio.wait_for(release.wait(), RACE_WAIT)

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(holding.wait(), RACE_WAIT)
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
