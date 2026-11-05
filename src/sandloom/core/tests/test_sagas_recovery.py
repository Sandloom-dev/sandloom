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
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.testing import sagas as saga_testing
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.testing.infra import (
    has_serial_writers,
    override_instance,
    skip_unless_db_feature,
)
from sandloom.core.tests.saga_fixtures import (
    BILLING,
    MAIL,
    DurableLedger,
    EnrollInput,
    Ledger,
    charge_args,
    command_records,
    committed_effects,
    create_effect_tables,
    durable_balance,
    enroll_input,
    enroll_saga,
    install_fakes,
    notify_args,
    refund_args,
    step_status,
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


async def test_nondeterministic_args_after_a_crash_never_apply_the_step_twice(
    saga_harness, db_engine: Any
) -> None:
    """The step's provider applied, the transition crashed, the redrive calls
    the step again with OTHER arguments (G-SAGA-04, G-SAGA-07).

    Where the provider commits on its own (PostgreSQL), the key is already in
    the command log: the other arguments hit its conflict and the instance
    parks. On a serial-writer database the provider's effect, its record and
    the step's state commit in one transaction (G-UOW-09), so a provider
    commit that survives the caller's crash cannot happen there: the crash
    lost all three, the redrive's arguments are the only ones ever applied,
    and the step applies once. The branch follows the database's fact
    (``serial_writers``), never its name."""
    await create_effect_tables(db_engine)
    install_fakes(saga_harness, DurableLedger())
    saga_harness.crash_after("charge")  # applied, then re-called with OTHER args

    run = await saga_harness.run(DRIFTING, input=enroll_input())

    key = f"saga:{run.instance.saga_type}:{run.saga_id}:charge:do"
    executions = saga_harness.fake_calls[f"{BILLING}.spend"]
    assert run.crashes == 1
    assert run.calls[:2] == ["charge:do", "charge:do"]
    if not has_serial_writers(db_engine):
        assert run.status == "parked"
        assert run.park_reason == "nondeterministic_args"
        assert run.instance.last_error["code"] == "contract_idempotency_conflict"
        assert [c["amount"] for c in executions] == await committed_effects(
            db_engine, key
        )  # the first arguments' effect, once
    else:
        assert run.status == "completed"
        assert len(executions) == 2  # the first run was lost with the crash
        assert await committed_effects(db_engine, key) == [executions[1]["amount"]]
        assert await command_records(db_engine, f"{BILLING}.spend", key) == 1


async def test_nondeterministic_args_on_a_reissued_key_conflict_and_park(
    saga_harness, db_engine: Any
) -> None:
    """G-SAGA-04 on every database: a step whose provider committed and whose
    caller then failed WITHOUT losing the transition (a timeout after the
    provider ran: the attempt and, on a serial writer, the record commit with
    it) is re-issued with the same key and other arguments: the command log
    refuses it and the instance parks."""
    await create_effect_tables(db_engine)
    install_fakes(saga_harness, DurableLedger())
    saga_harness.fail_step("charge", error="timeout")

    run = await saga_harness.run(DRIFTING, input=enroll_input())

    key = f"saga:{run.instance.saga_type}:{run.saga_id}:charge:do"
    assert run.calls[:2] == ["charge:do", "charge:do"]
    assert run.status == "parked"
    assert run.park_reason == "nondeterministic_args"
    assert run.instance.last_error["code"] == "contract_idempotency_conflict"
    assert len(await committed_effects(db_engine, key)) == 1


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
# The sweep's nudge meets the step's open row lock: concurrent writers,
# which SQLite (one writer) does not have; the PostgreSQL lane runs it.
@skip_unless_db_feature("concurrent_writers")
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
        await asyncio.wait_for(finish.wait(), 30.0)
        return await ledger.spend(customer_id, amount, operation_key)

    saga_harness.fake_contract(BILLING, spend=slow_spend, reverse=ledger.reverse)
    saga_harness.fake_contract(MAIL, send=ledger.send)
    handle = await saga_harness.start(ENROLL, saga_id="race-1", input=enroll_input())
    reg = saga_harness.register(ENROLL)
    row = await saga_harness.instance(reg, handle.saga_id)

    step_task = asyncio.create_task(saga_harness.deliver(reg, row, 0))
    await asyncio.wait_for(started.wait(), 30.0)
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


async def test_a_crash_after_the_effect_is_never_reissued_after_the_ttl(
    saga_harness, db_engine: Any, monkeypatch: Any
) -> None:
    """The provider's effect ran, then the process died before our transition
    committed. A two-day outage later, with a 24 h dedupe TTL, the step is
    never re-issued (G-SAGA-12, G-SAGA-07).

    Where the provider commits on its own (PostgreSQL), the effect and the
    durable "started" marker survive: recovery reads the marker's time and
    parks instead of re-issuing the command. On a serial-writer database the
    effect, its record and the marker commit with the transition (G-UOW-09),
    so the scenario "the provider committed, the caller did not" cannot
    exist there: the crash lost all of them, the step never applied, and the
    expired deadline compensates without re-issuing it (its compensation
    finds nothing to undo). The branch follows ``serial_writers``."""
    await create_effect_tables(db_engine)
    ledger = DurableLedger()
    install_fakes(saga_harness, ledger)
    monkeypatch.setattr(
        saga_harness.core.settings, "contract_command_dedupe_ttl_seconds", 86_400
    )
    serial = has_serial_writers(db_engine)
    saga_harness.crash_after("charge")
    reg = saga_harness.register(ENROLL)
    handle = await saga_harness.start(ENROLL, saga_id="ttl-1", input=enroll_input())
    key = f"saga:{reg.saga_type}:{handle.saga_id}:charge:do"
    row = await saga_harness.instance(reg, handle.saga_id)
    with pytest.raises(saga_testing.SimulatedCrash):
        await saga_harness.deliver(reg, row, row.version)
    if not serial:
        assert ledger.balance == 70  # the provider DID commit
        assert await committed_effects(db_engine, key) == [30]
        assert await step_status(db_engine, key) == "started"
    else:  # all or nothing: the effect, its record and the marker are lost
        assert await committed_effects(db_engine, key) == []
        assert await command_records(db_engine, f"{BILLING}.spend", key) == 0
        assert await step_status(db_engine, key) is None
    assert (await saga_harness.instance(reg, handle.saga_id)).attempt == 0
    saga_harness.clock.advance(timedelta(days=2))

    run = await saga_harness.drive(ENROLL, handle.saga_id)

    assert "charge:do" not in run.calls  # never re-issued
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1
    if not serial:
        assert run.status == "parked"
        assert run.park_reason == "outcome_unknown_after_ttl"
        assert ledger.balance == 70
        assert await durable_balance(db_engine, handle.saga_id) == 70
    else:
        assert run.status == "compensated"
        assert await durable_balance(db_engine, handle.saga_id) == 100


# --------------------------------------------------------------------------- #
# a provider elsewhere: its unit never joins the transition's transaction
# --------------------------------------------------------------------------- #
ELSEWHERE = "sagaelsewhere"


@pytest.fixture
async def elsewhere(saga_harness: Any, db_engine: Any, tmp_path: Any) -> Any:
    """A second database instance whose units commit on their own, as a
    provider over RPC does. On a serial-writer database it is a second file
    (a unit on the same file would join the transition); on a database with
    concurrent writers, the test database through an instance of its own."""
    url = db_engine.url
    if has_serial_writers(db_engine):
        url = url.set(database=str(tmp_path / "elsewhere.db"))
    async with override_instance(
        saga_harness.core,
        "database",
        ELSEWHERE,
        driver="sqlalchemy",
        url=url.render_as_string(hide_password=False),
    ):
        engine = saga_harness.core.database(ELSEWHERE).engine
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: ContractCommandRecord.metadata.create_all(
                    c, tables=[ContractCommandRecord.__table__]
                )
            )
        await create_effect_tables(engine)
        yield engine


async def _crash_after_a_provider_elsewhere_committed(
    saga_harness: Any, db_engine: Any, elsewhere: Any
) -> tuple[str, str]:
    """Start ENROLL with the billing provider elsewhere, and crash right
    after its charge committed there: ``(saga_id, the charge's key)``."""
    install_fakes(saga_harness, DurableLedger(), database=ELSEWHERE)
    saga_harness.crash_after("charge")
    spend = saga_harness.fake_calls.setdefault(f"{BILLING}.spend", [])
    run = await saga_harness.run(
        ENROLL, input=enroll_input(), stop_when=lambda _row: bool(spend)
    )
    key = f"saga:{run.instance.saga_type}:{run.saga_id}:charge:do"
    assert run.crashes == 1
    # a serial writer committed the marker alone first, then called at once
    marked = 1 if has_serial_writers(db_engine) else 0
    assert run.transitions.count(saga_runtime.MARKED) == marked
    assert await committed_effects(elsewhere, key) == [30]  # it committed there
    assert await command_records(elsewhere, f"{BILLING}.spend", key) == 1
    # the started marker survived the crash: it committed before the call
    assert await step_status(db_engine, key) == "started"
    return run.saga_id, key


async def test_a_deadline_after_a_crash_resolves_a_provider_elsewhere_first(
    saga_harness, db_engine: Any, elsewhere: Any
) -> None:
    """G-SAGA-11 for a provider that commits on its own (another instance,
    or RPC), on every database: the transition crashed after the provider
    committed, the deadline passed. The runtime re-issues the step with the
    same key first (replayed from the provider's command log) and then
    compensates the applied charge. On a serial-writer database the marker
    commits in a transition of its own before the call (``marked``), since
    the transition that calls would take it down with it."""
    saga_id, key = await _crash_after_a_provider_elsewhere_committed(
        saga_harness, db_engine, elsewhere
    )
    saga_harness.clock.advance(timedelta(hours=1))  # past the 30 min deadline

    run = await saga_harness.drive(ENROLL, saga_id)

    assert run.status == "compensated"
    assert run.calls == ["charge:do", "charge:undo"]  # resolved, then undone
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1  # a replay
    assert await committed_effects(elsewhere, key) == [30]
    assert await durable_balance(elsewhere, saga_id) == 100


async def test_a_provider_elsewhere_is_never_reissued_after_the_ttl(
    saga_harness, db_engine: Any, elsewhere: Any, monkeypatch: Any
) -> None:
    """G-SAGA-12 for a provider that commits on its own, on every database:
    two days after the crash, with a 24 h dedupe TTL, recovery reads the
    surviving marker's time and parks instead of re-issuing the command."""
    monkeypatch.setattr(
        saga_harness.core.settings, "contract_command_dedupe_ttl_seconds", 86_400
    )
    saga_id, key = await _crash_after_a_provider_elsewhere_committed(
        saga_harness, db_engine, elsewhere
    )
    saga_harness.clock.advance(timedelta(days=2))

    run = await saga_harness.drive(ENROLL, saga_id)

    assert run.status == "parked"
    assert run.park_reason == "outcome_unknown_after_ttl"
    assert "charge:do" not in run.calls  # never re-issued
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1
    assert await committed_effects(elsewhere, key) == [30]


async def test_a_provider_elsewhere_completes_with_one_effect_and_a_first_attempt(
    saga_harness, db_engine: Any, elsewhere: Any
) -> None:
    """No fault: on a serial-writer database the marker commits alone
    (``marked``) and the call follows at once, uncounted: the first call is
    attempt 1, as on PostgreSQL (no ``marked`` there)."""
    install_fakes(saga_harness, DurableLedger(), database=ELSEWHERE)

    run = await saga_harness.run(ENROLL, input=enroll_input(price=30))

    key = f"saga:{run.instance.saga_type}:{run.saga_id}:charge:do"
    marked = 1 if has_serial_writers(db_engine) else 0
    assert run.status == "completed"
    assert run.transitions.count(saga_runtime.MARKED) == marked
    assert run.transitions.count(saga_runtime.ADVANCED) == 3
    assert run.calls == ["charge:do", "grant_access:do", "notify:do"]
    assert run.step_row("charge")["attempts"] == 1
    assert await committed_effects(elsewhere, key) == [30]
    assert await durable_balance(elsewhere, run.saga_id) == 70


async def test_a_marked_step_is_called_in_the_same_delivery(
    saga_harness, db_engine: Any, elsewhere: Any, monkeypatch: Any
) -> None:
    """The marked transition's next one runs right after its COMMIT, not at
    the next delivery: a delivery delayed past the TTL (or the deadline)
    never finds a marker without a call, and parks or compensates nothing."""
    monkeypatch.setattr(
        saga_harness.core.settings, "contract_command_dedupe_ttl_seconds", 86_400
    )
    install_fakes(saga_harness, DurableLedger(), database=ELSEWHERE)
    reg = saga_harness.register(ENROLL)
    handle = await saga_harness.start(ENROLL, saga_id="mk-1", input=enroll_input())
    key = f"saga:{reg.saga_type}:{handle.saga_id}:charge:do"
    row = await saga_harness.instance(reg, handle.saga_id)

    first = await saga_harness.deliver(reg, row, row.version)

    serial = has_serial_writers(db_engine)
    assert first == (saga_runtime.MARKED if serial else saga_runtime.ADVANCED)
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1  # already called
    assert await step_status(db_engine, key) == "succeeded"
    assert await committed_effects(elsewhere, key) == [30]
    saga_harness.clock.advance(timedelta(days=2))  # past the TTL and the deadline

    run = await saga_harness.drive(ENROLL, handle.saga_id)

    # the applied charge (a known outcome, before the pivot) is compensated
    # once the deadline passed, as on PostgreSQL; nothing is parked
    assert run.status == "compensated"
    assert run.calls == ["charge:undo"]
    assert len(saga_harness.fake_calls[f"{BILLING}.spend"]) == 1
    assert await durable_balance(elsewhere, handle.saga_id) == 100


@pytest.mark.parametrize("outage", [timedelta(0), timedelta(days=2)])
async def test_a_marker_left_without_a_call_is_a_counted_attempt(
    saga_harness, db_engine: Any, elsewhere: Any, monkeypatch: Any, outage: Any
) -> None:
    """The process died after the marker committed and before the call
    (``crash_before``: on a serial-writer database inside the transition run
    after ``marked``). The redelivered nudge finds a marker that is not its
    own: a counted transient attempt, as on PostgreSQL. Within the TTL the
    call is attempt 2; past it the step is parked, never re-issued blindly
    (G-SAGA-12)."""
    monkeypatch.setattr(
        saga_harness.core.settings, "contract_command_dedupe_ttl_seconds", 86_400
    )
    install_fakes(saga_harness, DurableLedger(), database=ELSEWHERE)
    saga_harness.crash_before("charge")
    spend = saga_harness.fake_calls.setdefault(f"{BILLING}.spend", [])
    seen: list[int] = []

    def one_delivery(row: Any) -> bool:
        seen.append(row.version)
        return len(seen) > 1

    run = await saga_harness.run(ENROLL, input=enroll_input(), stop_when=one_delivery)
    key = f"saga:{run.instance.saga_type}:{run.saga_id}:charge:do"
    assert run.crashes == 1 and spend == []
    assert await step_status(db_engine, key) == "started"  # the marker alone
    saga_harness.clock.advance(outage)

    rest = await saga_harness.drive(ENROLL, run.saga_id)

    if outage:
        assert rest.status == "parked"
        assert rest.park_reason == "outcome_unknown_after_ttl"
        assert "charge:do" not in rest.calls and spend == []
    else:
        assert rest.status == "completed"
        assert rest.step_row("charge")["attempts"] == 2  # the lost one counted
        assert len(spend) == 1
        assert await committed_effects(elsewhere, key) == [30]
