"""Recovery rules (ADR-0008 §7): one test per cell of the outcome table, the
resolve-before-compensate rule and a timeout racing a completing step."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from sandloom.core.module_api import Saga, SagaContext, local_step, step
from sandloom.core.sagas import runtime as saga_runtime
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.testing import sagas as saga_testing
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import (
    BILLING,
    MAIL,
    EnrollInput,
    Ledger,
    charge_args,
    enroll_input,
    enroll_saga,
    install_fakes,
    notify_args,
    refund_args,
)
from sandloom.core.unit_of_work import db_session

ENROLL = enroll_saga("enroll_recovery")


def _ledger(saga_harness: Any) -> Ledger:
    ledger = Ledger()
    install_fakes(saga_harness, ledger)
    return ledger


async def _audit_actions(db_engine: Any) -> list[str]:
    async with db_engine.connect() as conn:
        rows = await conn.execute(select(AuditLogEntry.action, AuditLogEntry.data))
        return [str(a) for a, _data in rows.all()]


# --------------------------------------------------------------------------- #
# refused
# --------------------------------------------------------------------------- #
async def test_refused_before_the_pivot_compensates_the_applied_steps(
    saga_harness,
) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("grant_access", error="refused")

    run = await saga_harness.run(ENROLL, input=enroll_input(price=30))

    assert run.status == "compensated"
    assert run.calls == ["charge:do", "grant_access:do", "charge:undo"]
    run.assert_keys_derived()
    assert ledger.balance == 100
    assert run.step_row("grant_access")["status"] == "refused"


async def test_refused_first_step_compensates_nothing(saga_harness) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("charge", error="refused", status=402)

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "compensated"
    assert run.calls == ["charge:do"]
    assert ledger.balance == 100


async def test_refused_after_the_pivot_parks_with_an_audit_row(
    saga_harness, db_engine: Any
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="refused", status=404)

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "parked"
    assert run.park_reason == "retriable_refused"
    assert run.instance.parked_from == "running"
    assert "core.saga.parked" in await _audit_actions(db_engine)


async def test_refused_compensation_parks(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("grant_access", error="refused")
    saga_harness.fail_step("charge", error="refused", direction="undo")

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "parked"
    assert run.park_reason == "compensation_refused"
    assert run.instance.parked_from == "compensating"


# --------------------------------------------------------------------------- #
# transient
# --------------------------------------------------------------------------- #
async def test_transient_before_the_pivot_retries_with_full_jitter_backoff(
    saga_harness,
) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("charge", times=2, error="transient")
    start = saga_harness.clock.now

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "completed"
    assert run.calls[:3] == ["charge:do"] * 3
    assert run.step_row("charge")["attempts"] == 3
    # base 1 s, x2 per attempt, jitter fixed at 0.5: 0.5 s + 1 s
    assert saga_harness.clock.now - start == timedelta(seconds=1.5)
    assert ledger.balance == 70


async def test_transient_until_the_deadline_before_the_pivot_compensates(
    saga_harness,
) -> None:
    ledger = _ledger(saga_harness)
    # a local step's failure is never ambiguous: nothing to resolve
    saga_harness.fail_step("grant_access", times=10_000, error="transient")

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "compensated"
    assert run.calls[-1] == "charge:undo"
    assert "grant_access:undo" not in run.calls
    assert ledger.balance == 100


async def test_transient_after_the_pivot_parks_at_the_deadline(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("notify", times=10_000, error="transient")

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "parked"
    assert run.park_reason == "deadline_exceeded"
    assert "charge:undo" not in run.calls  # past the pivot: forward only


async def test_transient_compensation_parks_at_the_compensation_deadline(
    saga_harness,
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("grant_access", error="refused")
    saga_harness.fail_step("charge", times=10_000, direction="undo")

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.status == "parked"
    assert run.park_reason == "compensation_deadline_exceeded"
    assert run.step_row("charge", "undo")["attempts"] > 1


# --------------------------------------------------------------------------- #
# conflict / misconfigured
# --------------------------------------------------------------------------- #
_COUNTER = {"n": 0}


def drifting_charge_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    _COUNTER["n"] += 1  # NOT a pure function of input/results
    return {"customer_id": c.input.customer_id, "amount": c.input.price + _COUNTER["n"]}


DRIFTING = Saga[EnrollInput](
    "enroll_drifting",
    version=1,
    input=EnrollInput,
    deadline=timedelta(minutes=30),
    steps=[
        step(
            "charge",
            call=f"{BILLING}.spend",
            args=drifting_charge_args,
            compensate=f"{BILLING}.reverse",
            compensate_args=refund_args,
        ),
        local_step("grant_access", kind="pivot", apply=lambda db, c: _none()),
    ],
)


async def _none() -> None:
    return None


async def test_nondeterministic_args_hit_the_command_log_conflict_and_park(
    saga_harness,
) -> None:
    _ledger(saga_harness)
    saga_harness.crash_after("charge")  # applied, then re-called with OTHER args

    run = await saga_harness.run(DRIFTING, input=enroll_input())

    assert run.status == "parked"
    assert run.park_reason == "nondeterministic_args"
    assert run.instance.last_error["code"] == "contract_idempotency_conflict"


def misnamed_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer": c.input.customer_id, "amount": c.input.price}


MISWIRED = Saga[EnrollInput](
    "enroll_miswired",
    version=1,
    input=EnrollInput,
    deadline=timedelta(minutes=30),
    steps=[
        step(
            "charge",
            call=f"{BILLING}.spend",
            args=misnamed_args,
            compensate=f"{BILLING}.reverse",
            compensate_args=refund_args,
            check_args=False,  # escapes the startup check: the binding layer catches it
        ),
        local_step("grant_access", kind="pivot", apply=lambda db, c: _none()),
    ],
)


async def test_argument_binding_error_parks_as_misconfigured(saga_harness) -> None:
    _ledger(saga_harness)

    run = await saga_harness.run(MISWIRED, input=enroll_input())

    assert run.status == "parked"
    assert run.park_reason == "misconfigured"
    assert run.instance.last_error["code"] == "contract_argument_invalid"


async def test_401_parks_as_misconfigured(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("charge", error="misconfigured")

    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert run.park_reason == "misconfigured"


# --------------------------------------------------------------------------- #
# deadlines
# --------------------------------------------------------------------------- #
async def test_deadline_before_the_pivot_starts_compensation(saga_harness) -> None:
    _ledger(saga_harness)
    handle = await saga_harness.start(ENROLL, saga_id="dl-1", input=enroll_input())
    saga_harness.clock.advance(ENROLL.deadline + timedelta(seconds=1))

    run = await saga_harness.drive(ENROLL, handle.saga_id)

    assert run.status == "compensated"
    assert run.calls == []  # nothing applied, nothing to undo


async def test_deadline_after_the_pivot_parks(saga_harness) -> None:
    _ledger(saga_harness)
    run = await saga_harness.run(
        ENROLL,
        input=enroll_input(),
        stop_when=lambda row: row.step_index == 2,
    )
    saga_harness.clock.advance(ENROLL.deadline)

    run = await saga_harness.drive(ENROLL, run.saga_id)

    assert run.status == "parked" and run.park_reason == "deadline_exceeded"


async def test_compensation_deadline_parks_a_backward_instance(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("grant_access", error="refused")
    saga_harness.fail_step("charge", times=1, direction="undo")
    run = await saga_harness.run(
        ENROLL,
        input=enroll_input(),
        stop_when=lambda row: row.status == "compensating" and row.attempt > 0,
    )
    saga_harness.clock.advance(timedelta(hours=2))

    run = await saga_harness.drive(ENROLL, run.saga_id)

    assert run.status == "parked"
    assert run.park_reason == "compensation_deadline_exceeded"


# --------------------------------------------------------------------------- #
# resolve before compensating
# --------------------------------------------------------------------------- #
async def test_ambiguous_step_is_resolved_before_it_is_compensated(
    saga_harness,
) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("charge", error="timeout")  # applied, caller times out
    run = await saga_harness.run(
        ENROLL,
        input=enroll_input(price=30),
        stop_when=lambda row: row.attempt > 0,
    )
    assert ledger.balance == 70  # the provider DID commit
    saga_harness.clock.advance(ENROLL.deadline + timedelta(seconds=1))

    run = await saga_harness.drive(ENROLL, run.saga_id)

    assert run.status == "compensated"
    # re-issued with the SAME key (replayed), then compensated as applied
    assert run.calls == ["charge:do", "charge:undo"]
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1
    assert ledger.balance == 100


async def test_resolve_that_finds_a_refusal_compensates_nothing(saga_harness) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("charge", error="transient")  # never reached the provider
    run = await saga_harness.run(
        ENROLL, input=enroll_input(), stop_when=lambda row: row.attempt > 0
    )
    saga_harness.clock.advance(ENROLL.deadline + timedelta(seconds=1))
    saga_harness.fail_step("charge", error="refused")  # the resolve call refuses

    run = await saga_harness.drive(ENROLL, run.saga_id)

    assert run.status == "compensated"
    assert run.calls == ["charge:do"]  # the resolve, no undo
    assert ledger.balance == 100


async def test_resolve_one_liner_on_the_kernel_saga(saga_harness) -> None:
    _ledger(saga_harness)
    await saga_harness.assert_resolves_before_compensating(ENROLL, input=enroll_input())


async def test_compensation_idempotence_one_liner(saga_harness) -> None:
    _ledger(saga_harness)
    await saga_harness.assert_compensations_idempotent(ENROLL, input=enroll_input())


def pivot_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "template": "activate"}


COMMAND_PIVOT = Saga[EnrollInput](
    "enroll_command_pivot",
    version=1,
    input=EnrollInput,
    deadline=timedelta(minutes=30),
    steps=[
        step(
            "charge",
            call=f"{BILLING}.spend",
            args=charge_args,
            compensate=f"{BILLING}.reverse",
            compensate_args=refund_args,
        ),
        step("activate", kind="pivot", call=f"{MAIL}.send", args=pivot_args),
        step("notify", call=f"{MAIL}.send", args=notify_args),
    ],
)


async def test_resolved_pivot_that_applied_never_compensates(saga_harness) -> None:
    ledger = _ledger(saga_harness)
    saga_harness.fail_step("activate", error="timeout")
    run = await saga_harness.run(
        COMMAND_PIVOT,
        input=enroll_input(),
        stop_when=lambda row: row.step_index == 1 and row.attempt > 0,
    )
    saga_harness.clock.advance(COMMAND_PIVOT.deadline + timedelta(seconds=1))

    run = await saga_harness.drive(COMMAND_PIVOT, run.saga_id)

    # the pivot had applied: forward recovery only (and the deadline passed)
    assert "charge:undo" not in run.calls
    assert run.status == "parked" and run.park_reason == "deadline_exceeded"
    assert ledger.balance == 70


# --------------------------------------------------------------------------- #
# timeout racing completion
# --------------------------------------------------------------------------- #
async def test_timeout_racing_a_completing_step_is_serialized_by_the_row_lock(
    saga_harness,
) -> None:
    """The step completes while the deadline passes: first commit wins.

    A sweep nudge arriving during the slow step finds the row locked (no-op);
    the step's transition commits first; the deadline transition then sees
    the new state and compensates the step AS APPLIED.
    """
    ledger = Ledger()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_spend(
        customer_id: uuid.UUID, amount: int, operation_key: str
    ) -> int:
        started.set()
        await finish.wait()
        return await ledger.spend(customer_id, amount, operation_key)

    saga_harness.fake_contract(BILLING, spend=slow_spend, reverse=ledger.reverse)
    saga_harness.fake_contract(MAIL, send=ledger.send)
    handle = await saga_harness.start(ENROLL, saga_id="race-1", input=enroll_input())
    reg = saga_harness.register(ENROLL)
    row = await saga_harness.instance(reg, handle.saga_id)

    step_task = asyncio.create_task(saga_harness.deliver(reg, row, 0))
    await started.wait()
    saga_harness.clock.advance(ENROLL.deadline + timedelta(seconds=1))
    # the timer fires while the step runs: the row is locked
    assert await saga_harness.deliver(reg, row, 0) == saga_runtime.NOOP_LOCKED
    finish.set()
    assert await step_task == saga_runtime.ADVANCED

    run = await saga_harness.drive(ENROLL, handle.saga_id)
    assert run.status == "compensated"
    assert run.calls == ["charge:undo"]
    assert ledger.balance == 100


async def test_ttl_guard_parks_an_ambiguous_step_whose_record_may_be_gone(
    saga_harness, monkeypatch: Any
) -> None:
    _ledger(saga_harness)
    monkeypatch.setattr(
        saga_harness.core.settings, "contract_command_dedupe_ttl_seconds", 600
    )
    saga_harness.fail_step("charge", error="timeout")
    run = await saga_harness.run(
        ENROLL, input=enroll_input(), stop_when=lambda row: row.attempt > 0
    )
    saga_harness.clock.advance(timedelta(minutes=15))  # < deadline, > TTL

    run = await saga_harness.drive(ENROLL, run.saga_id)

    assert run.status == "parked"
    assert run.park_reason == "outcome_unknown_after_ttl"


async def test_status_reads_the_handle(saga_harness) -> None:
    _ledger(saga_harness)
    run = await saga_harness.run(ENROLL, saga_id="st-1", input=enroll_input())
    async with db_session():
        handle = await saga_harness.runtime.status(ENROLL, "st-1")
    assert handle is not None and handle.status == "completed" and handle.step is None
    assert run.status == "completed"


async def test_a_crash_after_the_provider_committed_is_never_reissued_after_the_ttl(
    saga_harness, monkeypatch: Any
) -> None:
    """The provider committed, then the process died before our transition
    committed (only the durable "started" marker survives). A two-day outage
    later, with a 24 h dedupe TTL, recovery reads the marker's time and parks
    instead of re-issuing the command (G-SAGA-12)."""
    ledger = _ledger(saga_harness)
    monkeypatch.setattr(
        saga_harness.core.settings, "contract_command_dedupe_ttl_seconds", 86_400
    )
    saga_harness.crash_after("charge")
    reg = saga_harness.register(ENROLL)
    handle = await saga_harness.start(ENROLL, saga_id="ttl-1", input=enroll_input())
    row = await saga_harness.instance(reg, handle.saga_id)
    with pytest.raises(saga_testing.SimulatedCrash):
        await saga_harness.deliver(reg, row, row.version)
    assert ledger.balance == 70  # the provider DID commit
    assert (await saga_harness.instance(reg, handle.saga_id)).attempt == 0
    saga_harness.clock.advance(timedelta(days=2))

    run = await saga_harness.drive(ENROLL, handle.saga_id)

    assert run.status == "parked"
    assert run.park_reason == "outcome_unknown_after_ttl"
    assert "charge:do" not in run.calls  # never re-issued
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1
    assert ledger.balance == 70
