"""Saga laws (spec §8.2; ADR-0008 §6-§7): step idempotence and forward-only
status, over generated fault schedules driven through the REAL saga protocol
(``sandloom.core.testing.sagas``: row lock, outcome table, transitions, outbox
nudges, the durable command log of every command step).

The saga: ``charge`` (command, compensatable) -> ``reserve`` (local,
compensatable: a row in the transition's unit) -> ``grant`` (command, the
pivot) -> ``notify`` (command, retriable). Hypothesis draws a list of
operations — ``fail_step`` (transient / refused / timeout, forward or
backward), ``crash_before`` / ``crash_after``, ``deliver_twice`` and jumps of
the clock past the deadline at a given transition — runs the saga until it
settles, and records every instance state the driver observed.

Properties:

* **step idempotence** — however often a step is delivered, retried,
  replayed after a crash or re-issued to resolve an ambiguous outcome, its
  effect happens at most once: every provider body runs at most once per
  operation key, the local step's row exists at most once, and the final
  effects match the final status (completed: every step once; compensated:
  nothing remains and the pivot never ran);
* **forward-only status** — the version never decreases; the status never
  moves to an earlier phase (``running`` < ``compensating`` < terminal),
  with ONE documented exception: resolve-before-compensate on the pivot that
  turns out applied returns ``compensating`` -> ``running`` (ADR-0008 §7,
  "past the go/no-go point there is only forward recovery"); within a phase
  the step index only moves in that phase's direction; a terminal instance
  stays terminal and unchanged when every old nudge is redelivered.

Counterexample found and fixed (kept as explicit examples and the regression
``test_a_deadline_after_a_crash_resolves_the_step_before_compensating``): a
crash after a command step's provider committed, then the deadline passing,
compensated without resolving the step, so its effect stayed (``charge``) or
an applied pivot was compensated around (``grant``). The runtime now resolves
every forward command step before compensating on a deadline.
Schedules no real run can produce (a refusal injected on a re-issue of a call
whose provider already committed) are filtered out (``_realistic``).
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
)

from sandloom.core.descriptors import BACKWARD, FORWARD, Direction
from sandloom.core.module_api import Saga, SagaContext, local_step, step
from sandloom.core.sagas import runtime as saga_runtime
from sandloom.core.sql_alchemy.saga_store import InstanceRow
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.sagas import open_saga_harness

BILLING = "lawsbilling.credits.v1"
ACCESS = "lawsaccess.grants.v1"
MAIL = "lawsmail.outbox.v1"
PRICE = 30
START_BALANCE = 100

_METADATA = MetaData()
RESERVATIONS = Table(
    "laws_saga_reservations",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("saga_id", String(100), nullable=False, index=True),
)


class LawsInput(BaseModel):
    customer_id: uuid.UUID
    price: int


class Ledger:
    """In-memory provider state of one example (the providers are fakes)."""

    def __init__(self) -> None:
        self.balance = START_BALANCE
        self.charges: dict[str, int] = {}

    async def spend(
        self, customer_id: uuid.UUID, amount: int, operation_key: str
    ) -> int:
        self.balance -= amount
        self.charges[operation_key] = amount
        return self.balance

    async def reverse(
        self,
        customer_id: uuid.UUID,
        amount: int,
        of_operation_key: str,
        operation_key: str,
    ) -> int:
        self.balance += self.charges.pop(of_operation_key, 0)  # nothing to undo is ok
        return self.balance

    async def grant(self, customer_id: uuid.UUID, operation_key: str) -> str:
        return f"grant-{operation_key[-8:]}"

    async def send(
        self, customer_id: uuid.UUID, template: str, operation_key: str
    ) -> str:
        return "sent"


def _charge_args(c: SagaContext[LawsInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "amount": c.input.price}


def _refund_args(c: SagaContext[LawsInput]) -> dict[str, object]:
    return {
        "customer_id": c.input.customer_id,
        "amount": c.input.price,
        "of_operation_key": c.key("charge"),
    }


def _grant_args(c: SagaContext[LawsInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id}


def _notify_args(c: SagaContext[LawsInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "template": "granted"}


async def _reserve(db: Any, c: SagaContext[LawsInput]) -> dict[str, object]:
    await db.execute(insert(RESERVATIONS).values(saga_id=c.saga_id))
    return {"reserved": True}


async def _unreserve(db: Any, c: SagaContext[LawsInput]) -> None:
    await db.execute(delete(RESERVATIONS).where(RESERVATIONS.c.saga_id == c.saga_id))


STEP_NAMES = ("charge", "reserve", "grant", "notify")
PIVOT_INDEX = STEP_NAMES.index("grant")
COMPENSATABLE = ("charge", "reserve")

LAWS_SAGA = Saga[LawsInput](
    "laws_grant",
    version=1,
    input=LawsInput,
    deadline=timedelta(minutes=30),
    steps=[
        step(
            "charge",
            call=f"{BILLING}.spend",
            args=_charge_args,
            compensate=f"{BILLING}.reverse",
            compensate_args=_refund_args,
        ),
        local_step("reserve", apply=_reserve, compensate=_unreserve),
        step("grant", call=f"{ACCESS}.grant", args=_grant_args, kind="pivot"),
        step("notify", call=f"{MAIL}.send", args=_notify_args),
    ],
)

# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fail:
    step: str
    error: str
    direction: Direction
    times: int


@dataclass(frozen=True)
class Crash:
    when: str  # "before" | "after"
    step: str
    direction: Direction


@dataclass(frozen=True)
class Twice:
    pass


@dataclass(frozen=True)
class JumpPastDeadline:
    """The clock passes the saga deadline at the n-th observed state."""

    at_observation: int


@dataclass(frozen=True)
class DeadlineDuringBackoff:
    """The deadline passes while the n-th transient retry is pending (the
    ambiguous case: the runtime must resolve before compensating)."""

    nth: int


def _direction_for(step_name: str) -> st.SearchStrategy[Direction]:
    if step_name in COMPENSATABLE:
        return st.sampled_from([FORWARD, BACKWARD])
    return st.just(FORWARD)


FAILS = st.sampled_from(STEP_NAMES).flatmap(
    lambda name: st.builds(
        Fail,
        step=st.just(name),
        error=st.sampled_from(["transient", "refused", "timeout"]),
        direction=_direction_for(name),
        times=st.integers(min_value=1, max_value=3),
    )
)
CRASHES = st.sampled_from(STEP_NAMES).flatmap(
    lambda name: st.builds(
        Crash,
        when=st.sampled_from(["before", "after"]),
        step=st.just(name),
        direction=_direction_for(name),
    )
)


def _realistic(ops: list[Any]) -> bool:
    """The harness injects a refusal BEFORE the call. Once a step's provider
    has committed (a ``timeout`` fault or ``crash_after``), a re-issue with the
    same key replays the committed outcome through the command log and can no
    longer be refused, so a schedule combining both on one step and direction
    models no real run."""
    committed = {
        (op.step, op.direction)
        for op in ops
        if (isinstance(op, Fail) and op.error == "timeout")
        or (isinstance(op, Crash) and op.when == "after")
    }
    return not any(
        isinstance(op, Fail)
        and op.error == "refused"
        and (op.step, op.direction) in committed
        for op in ops
    )


OPS = st.lists(
    st.one_of(
        FAILS,
        CRASHES,
        st.just(Twice()),
        st.builds(JumpPastDeadline, at_observation=st.integers(0, 12)),
        st.builds(DeadlineDuringBackoff, nth=st.integers(1, 3)),
    ),
    max_size=6,
).filter(_realistic)

RANK = {"running": 0, "compensating": 1, "completed": 2, "compensated": 2, "parked": 2}
TERMINAL = ("completed", "compensated", "parked")


@dataclass(frozen=True)
class Seen:
    status: str
    version: int
    step_index: int
    resolving: bool

    @classmethod
    def of(cls, row: InstanceRow) -> Seen:
        return cls(row.status, row.version, row.step_index, row.resolving)


@pytest.fixture
async def reservations(db_engine: Any) -> AsyncIterator[Any]:
    async with db_engine.begin() as conn:
        await conn.run_sync(_METADATA.create_all)
    yield db_engine


async def _reserved_rows(engine: Any, saga_id: str) -> int:
    async with engine.connect() as conn:
        stmt = (
            select(func.count())
            .select_from(RESERVATIONS)
            .where(RESERVATIONS.c.saga_id == saga_id)
        )
        return int((await conn.execute(stmt)).scalar_one())


def _assert_forward_only(seen: list[Seen]) -> None:
    for before, after in itertools.pairwise(seen):
        assert after.version >= before.version, seen
        if before.status in TERMINAL:
            assert after == before, ("a terminal instance moved", seen)
            continue
        if RANK[after.status] < RANK[before.status]:
            # the only way back: the pivot, resolved before compensating,
            # turned out applied -> forward recovery (ADR-0008 §7)
            assert (before.status, after.status) == ("compensating", "running"), seen
            assert before.resolving and before.step_index == PIVOT_INDEX, seen
            assert after.step_index == PIVOT_INDEX + 1, seen
            event("pivot resolved as applied: forward recovery")
            continue
        if after.status == before.status == "running":
            assert after.step_index >= before.step_index, seen
        if after.status == before.status == "compensating":
            assert after.step_index <= before.step_index, seen


def _assert_each_body_ran_at_most_once_per_key(
    fake_calls: dict[str, list[dict[str, Any]]],
) -> None:
    for label, calls in fake_calls.items():
        keys = [c["operation_key"] for c in calls]
        assert len(keys) == len(set(keys)), (label, keys)


def _crash_then_deadline(ops: list[Any]) -> bool:
    """The once-open gap (fixed; see the regression test): a crash after a
    forward command step's provider committed, then the deadline."""
    crash_after = any(
        isinstance(op, Crash)
        and op.when == "after"
        and op.direction == FORWARD
        and op.step in ("charge", "grant")
        for op in ops
    )
    jump = any(isinstance(op, JumpPastDeadline | DeadlineDuringBackoff) for op in ops)
    return crash_after and jump


SAGA_LAWS = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
#: Hypothesis' shrunk counterexample: ``charge`` runs at the provider, the
#: process dies before the transition commits (the attempt is rolled back with
#: it), then the deadline passes before the redelivery. The runtime sees an
#: untried step (``attempt == 0``), so it does not resolve before
#: compensating: the saga ends ``compensated`` with the charge still applied
#: (balance 70, not 100) — the "compensation overtakes a late forward" hole
#: ADR-0008 §7 says resolve-before-compensate closes.
CRASH_THEN_DEADLINE = [Crash("after", "charge", FORWARD), JumpPastDeadline(1)]
#: The same gap on the pivot: ``grant`` ran at the provider, the transition
#: crashed, the deadline passed -> ``charge`` and ``reserve`` are compensated
#: although the pivot is applied (an applied pivot is never compensated).
CRASH_AFTER_PIVOT_THEN_DEADLINE = [
    Crash("after", "grant", FORWARD),
    JumpPastDeadline(3),
]


@SAGA_LAWS
@given(ops=OPS)
@example(ops=CRASH_THEN_DEADLINE)
@example(ops=CRASH_AFTER_PIVOT_THEN_DEADLINE)
async def test_steps_are_idempotent_and_status_only_moves_forward(
    sandloom_core: Any, reservations: Any, ops: list[Any]
) -> None:
    await _check_saga_laws(sandloom_core, reservations, ops)


@SAGA_LAWS
@given(ops=st.sampled_from([CRASH_THEN_DEADLINE, CRASH_AFTER_PIVOT_THEN_DEADLINE]))
async def test_a_deadline_after_a_crash_resolves_the_step_before_compensating(
    sandloom_core: Any, reservations: Any, ops: list[Any]
) -> None:
    """Regression (G-SAGA, resolve-before-compensate): the provider committed
    the step, the transition crashed (its attempt rolled back), the deadline
    passed: the runtime re-issues the step with the same key first, so an
    applied charge is compensated and an applied pivot recovers forward."""
    await _check_saga_laws(sandloom_core, reservations, ops)


@SAGA_LAWS
@given(ops=OPS.filter(lambda ops: not _crash_then_deadline(ops)))
# the rare paths, always covered: the pivot resolved as applied (forward
# recovery), a refused compensation and a refused retriable step (parked)
@example(ops=[Fail("grant", "timeout", FORWARD, 1), DeadlineDuringBackoff(1)])
@example(
    ops=[
        Fail("reserve", "refused", FORWARD, 1),
        Fail("charge", "refused", BACKWARD, 1),
    ]
)
@example(ops=[Fail("notify", "refused", FORWARD, 1), Twice()])
async def test_saga_laws_hold_outside_the_crash_then_deadline_gap(
    sandloom_core: Any, reservations: Any, ops: list[Any]
) -> None:
    """The same laws on every schedule but the known gap above, so the rest
    of the property guarded while the gap was open (kept: a cheap second
    sampling of the common schedules)."""
    await _check_saga_laws(sandloom_core, reservations, ops)


async def _check_saga_laws(
    sandloom_core: Any, reservations: Any, ops: list[Any]
) -> None:
    async with open_saga_harness(sandloom_core) as harness:
        ledger = Ledger()
        harness.fake_contract(BILLING, spend=ledger.spend, reverse=ledger.reverse)
        harness.fake_contract(ACCESS, grant=ledger.grant)
        harness.fake_contract(MAIL, send=ledger.send)
        jumps: set[int] = set()
        backoff_jumps: set[int] = set()
        for op in ops:
            if isinstance(op, Fail):
                harness.fail_step(
                    op.step, times=op.times, error=op.error, direction=op.direction
                )
            elif isinstance(op, Crash):
                crash = (
                    harness.crash_before if op.when == "before" else harness.crash_after
                )
                crash(op.step, direction=op.direction)
            elif isinstance(op, Twice):
                harness.deliver_twice()
            elif isinstance(op, JumpPastDeadline):
                jumps.add(op.at_observation)
            else:
                backoff_jumps.add(op.nth)

        seen: list[Seen] = []
        backoffs = 0
        past_deadline = LAWS_SAGA.deadline + timedelta(minutes=1)

        def observe(row: InstanceRow) -> bool:
            nonlocal backoffs
            if len(seen) in jumps:
                harness.clock.advance(past_deadline)
            if row.attempt > 0 and row.last_outcome == "transient":
                backoffs += 1
                if backoffs in backoff_jumps:
                    harness.clock.advance(past_deadline)
            seen.append(Seen.of(row))
            return False

        saga_id = f"laws-{uuid.uuid4().hex}"
        run = await harness.run(
            LAWS_SAGA,
            saga_id=saga_id,
            input=LawsInput(customer_id=uuid.uuid4(), price=PRICE),
            stop_when=observe,
        )
        seen.append(Seen.of(run.instance))
        assert run.status in TERMINAL, run.status
        event(f"status={run.status} {run.park_reason or ''}".strip())

        # terminal stays terminal: every old nudge redelivered is a no-op
        reg = harness.register(LAWS_SAGA)
        for version in range(run.instance.version + 1):
            outcome = await harness.deliver(reg, run.instance, version)
            assert outcome == saga_runtime.NOOP_STALE, (version, outcome)
        seen.append(Seen.of(await harness.instance(reg, saga_id)))
        _assert_forward_only(seen)

        # step idempotence
        _assert_each_body_ran_at_most_once_per_key(harness.fake_calls)
        spent = len(harness.fake_calls[f"{BILLING}.spend"])
        granted = len(harness.fake_calls[f"{ACCESS}.grant"])
        sent = len(harness.fake_calls[f"{MAIL}.send"])
        reserved = await _reserved_rows(reservations, saga_id)
        assert spent <= 1 and granted <= 1 and sent <= 1 and reserved <= 1
        assert ledger.balance == START_BALANCE - PRICE * len(ledger.charges)
        if run.status == "completed":
            assert (spent, reserved, granted, sent) == (1, 1, 1, 1), run.calls
            assert ledger.balance == START_BALANCE - PRICE
        elif run.status == "compensated":
            # nothing remains, and an applied pivot is never compensated
            assert (reserved, granted, sent) == (0, 0, 0), run.calls
            assert ledger.balance == START_BALANCE
