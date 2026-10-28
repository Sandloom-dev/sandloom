"""Operator commands (ADR-0008 §8), start semantics (§4) and the timer sweep
(§5) — against the test database, through ``saga_harness``."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from sandloom.core.sagas import ops
from sandloom.core.sagas import runtime as saga_runtime
from sandloom.core.sagas.sweep import SagaSweeper
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.models.saga import SagaInstance
from sandloom.core.tenant_context import bind_tenant
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import (
    Ledger,
    enroll_input,
    enroll_saga,
    install_fakes,
)
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError

ENROLL = enroll_saga("enroll_ops")
APPLY = ops.OperatorIntent(apply=True, operator="alice", reason="incident 42")
DRY = ops.OperatorIntent(apply=False, operator="alice", reason="")


def _ledger(saga_harness: Any) -> Ledger:
    ledger = Ledger()
    install_fakes(saga_harness, ledger)
    return ledger


async def _audit(db_engine: Any, action: str) -> int:
    async with db_engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(AuditLogEntry)
                    .where(AuditLogEntry.action == action)
                )
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #
async def test_start_is_create_or_get_and_conflicts_on_other_input(
    saga_harness,
) -> None:
    _ledger(saga_harness)
    payload = enroll_input()
    first = await saga_harness.start(ENROLL, saga_id="biz-1", input=payload)
    again = await saga_harness.start(ENROLL, saga_id="biz-1", input=payload)
    assert first.created and not again.created
    assert again.instance_id == first.instance_id
    with pytest.raises(saga_runtime.SagaInputConflict):
        await saga_harness.start(ENROLL, saga_id="biz-1", input=enroll_input(price=1))


async def test_start_outside_a_unit_of_work_is_refused(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.register(ENROLL)
    with pytest.raises(UnitOfWorkRequiredError):
        await saga_harness.runtime.start(ENROLL, saga_id="x", input=enroll_input())


async def test_start_rejects_a_saga_id_that_breaks_keys(saga_harness) -> None:
    _ledger(saga_harness)
    with pytest.raises(ValueError, match="operation_key part"):
        await saga_harness.start(ENROLL, saga_id="a:b", input=enroll_input())


async def test_saga_state_never_crosses_tenants_without_rls(saga_harness) -> None:
    """Round 3 (G-SAGA-03): on a database without tenant RLS the runtime
    itself scopes saga state to the bound tenant. Tenant B asking for tenant
    A's business id gets nothing (not A's instance id, status or step), a
    tenant-less caller sees no tenant's saga, B cannot create-or-get A's
    instance, and a nudge naming another tenant never moves it."""
    _ledger(saga_harness)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    payload = enroll_input()
    first = await saga_harness.start(
        ENROLL, saga_id="iso-1", input=payload, tenant_id=tenant_a
    )
    async with db_session():
        with bind_tenant(tenant_b):
            assert await saga_harness.runtime.status(ENROLL, "iso-1") is None
        assert await saga_harness.runtime.status(ENROLL, "iso-1") is None
        with bind_tenant(tenant_a):
            own = await saga_harness.runtime.status(ENROLL, "iso-1")
    assert own is not None and own.instance_id == first.instance_id
    with pytest.raises(saga_runtime.SagaInputConflict):
        await saga_harness.start(
            ENROLL, saga_id="iso-1", input=payload, tenant_id=tenant_b
        )
    forged = {
        "payload": {
            "instance_id": str(first.instance_id),
            "expected_version": 0,
            "tenant_id": str(tenant_b),
        }
    }
    async with db_session() as session:
        result = await saga_harness.runtime.advance_nudge(session, forged)
    assert result == saga_runtime.NOOP_LOCKED
    row = await saga_harness.instance(ENROLL, "iso-1")
    assert (row.version, row.tenant_id) == (0, tenant_a)


async def test_single_tenant_status_and_start_see_the_canonical_tenants_saga(
    saga_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 4 (G-SAGA-03): under TENANCY_MODE=single the unit binds
    SINGLE_TENANT_ID, so an instance belongs to the canonical tenant. A
    status() called outside a unit opens its own and reads the tenant that
    unit bound (not the None seen before opening it); start's create-or-get
    and a nudge naming the canonical tenant reach the same instance."""
    _ledger(saga_harness)
    canonical = uuid.uuid4()
    core = saga_harness.runtime.core
    single = core.settings.model_copy(
        update={"tenancy_mode": "single", "single_tenant_id": canonical}
    )
    monkeypatch.setattr(core, "settings", single)
    payload = enroll_input()
    first = await saga_harness.start(ENROLL, saga_id="single-1", input=payload)
    row = await saga_harness.instance(ENROLL, "single-1")
    assert row.tenant_id == canonical
    handle = await saga_harness.runtime.status(ENROLL, "single-1")
    assert handle is not None and handle.instance_id == first.instance_id
    async with db_session():
        inside = await saga_harness.runtime.status(ENROLL, "single-1")
    assert inside is not None and inside.instance_id == first.instance_id
    again = await saga_harness.start(ENROLL, saga_id="single-1", input=payload)
    assert not again.created and again.instance_id == first.instance_id
    done = await saga_harness.drive(ENROLL, "single-1")
    assert done.status == "completed"


# --------------------------------------------------------------------------- #
# park / retry / resolve
# --------------------------------------------------------------------------- #
async def test_park_then_retry_resumes_with_audit_rows(saga_harness, db_engine) -> None:
    _ledger(saga_harness)
    run = await saga_harness.run(
        ENROLL, input=enroll_input(), stop_when=lambda row: row.step_index == 1
    )
    dry = await ops.park(saga_harness.runtime, ENROLL.saga_type, run.saga_id, DRY)
    assert not dry.applied and dry.after == "parked"
    assert (await saga_harness.instance(ENROLL, run.saga_id)).status == "running"

    parked = await ops.park(saga_harness.runtime, ENROLL.saga_type, run.saga_id, APPLY)
    assert parked.after == "parked"
    row = await saga_harness.instance(ENROLL, run.saga_id)
    assert row.park_reason == "operator"
    assert (await saga_harness.drive(ENROLL, run.saga_id)).status == "parked"

    retried = await ops.retry(
        saga_harness.runtime, ENROLL.saga_type, run.saga_id, APPLY
    )
    assert retried.after == "running"
    done = await saga_harness.drive(ENROLL, run.saga_id)
    assert done.status == "completed"
    assert await _audit(db_engine, "core.saga.parked") == 1
    assert await _audit(db_engine, "core.saga.retried") == 1


async def test_retry_refuses_an_unknown_outcome_after_the_dedupe_ttl(
    saga_harness, monkeypatch: Any
) -> None:
    """§8: outside the window with an unknown outcome, retry refuses."""
    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="timeout")
    run = await saga_harness.run(
        ENROLL,
        input=enroll_input(),
        stop_when=lambda row: row.step_index == 2 and row.attempt > 0,
    )
    await ops.park(saga_harness.runtime, ENROLL.saga_type, run.saga_id, APPLY)
    saga_harness.clock.advance(timedelta(days=2))  # beyond the 24 h command TTL

    with pytest.raises(ops.SagaOperationError) as info:
        await ops.retry(saga_harness.runtime, ENROLL.saga_type, run.saga_id, APPLY)
    assert "unknown outcome" in str(info.value)
    assert f"saga:{ENROLL.saga_type}:{run.saga_id}:notify:do" in str(info.value)
    assert "sandloom sagas resolve" in str(info.value)

    # the operator established it happened: resolve, and the saga continues
    resolved = await ops.resolve(
        saga_harness.runtime, ENROLL.saga_type, run.saga_id, "notify", "applied", APPLY
    )
    assert resolved.after == "completed"


async def test_retry_after_the_ttl_proceeds_when_the_outcome_is_known(
    saga_harness,
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="refused")
    run = await saga_harness.run(ENROLL, input=enroll_input())
    assert run.park_reason == "retriable_refused"
    saga_harness.clock.advance(timedelta(days=2))

    retried = await ops.retry(
        saga_harness.runtime, ENROLL.saga_type, run.saga_id, APPLY
    )
    assert retried.after == "running"
    assert (await saga_harness.drive(ENROLL, run.saga_id)).status == "completed"


async def test_resolve_skipped_compensation_moves_backward(
    saga_harness, db_engine
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("grant_access", error="refused")
    saga_harness.fail_step("charge", error="refused", direction="undo")
    run = await saga_harness.run(ENROLL, input=enroll_input())
    assert run.park_reason == "compensation_refused"

    with pytest.raises(ops.SagaOperationError, match="not the current step"):
        await ops.resolve(
            saga_harness.runtime,
            ENROLL.saga_type,
            run.saga_id,
            "notify",
            "applied",
            APPLY,
        )
    result = await ops.resolve(
        saga_harness.runtime, ENROLL.saga_type, run.saga_id, "charge", "skipped", APPLY
    )
    assert result.after == "compensated"
    assert await _audit(db_engine, "core.saga.step_resolved") == 1


async def test_resolve_requires_a_parked_instance(saga_harness) -> None:
    _ledger(saga_harness)
    run = await saga_harness.run(ENROLL, input=enroll_input())
    with pytest.raises(ops.SagaOperationError, match="completed"):
        await ops.resolve(
            saga_harness.runtime,
            ENROLL.saga_type,
            run.saga_id,
            "notify",
            "applied",
            APPLY,
        )


async def test_list_and_show(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="refused")
    run = await saga_harness.run(ENROLL, input=enroll_input())
    rows = await ops.list_instances(
        saga_harness.runtime,
        "default",
        ops._store.ListFilter(actionable_at=saga_harness.clock.now),
        operator="alice",
    )
    assert [r.saga_id for r in rows] == [run.saga_id]
    row, steps = await ops.show_instance(
        saga_harness.runtime, ENROLL.saga_type, run.saga_id, operator="alice"
    )
    assert row.park_reason == "retriable_refused"
    assert {(s["step"], s["direction"]) for s in steps} == {
        ("charge", "do"),
        ("grant_access", "do"),
        ("notify", "do"),
    }


# --------------------------------------------------------------------------- #
# the timer sweep
# --------------------------------------------------------------------------- #
async def _nudges(db_engine: Any) -> int:
    channel = f"{ENROLL.saga_type.split('.')[0]}.saga_advance.v1"
    async with db_engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(OutboxMessage.channel == channel)
                )
            ).scalar_one()
        )


async def test_sweep_nudges_due_instances_once_per_interval(
    saga_harness, db_engine
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("charge", error="transient")
    run = await saga_harness.run(
        ENROLL, input=enroll_input(), stop_when=lambda row: row.attempt > 0
    )
    assert run.transitions == [saga_runtime.RETRY_SCHEDULED]
    sweeper = SagaSweeper(saga_harness.runtime)
    before = await _nudges(db_engine)

    assert await sweeper.sweep_database("default") == 0  # backoff not elapsed
    saga_harness.clock.advance(timedelta(seconds=5))
    assert await sweeper.sweep_database("default") == 1
    assert await sweeper.sweep_database("default") == 0  # swept within the interval
    assert await _nudges(db_engine) == before + 1
    saga_harness.clock.advance(timedelta(seconds=6))
    assert await sweeper.sweep_database("default") == 1


async def test_sweep_drives_a_deadline_even_while_backing_off(saga_harness) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("charge", error="transient")
    run = await saga_harness.run(
        ENROLL, input=enroll_input(), stop_when=lambda row: row.attempt > 0
    )
    async with db_session() as session:
        await session.execute(
            update(SagaInstance)
            .where(SagaInstance.saga_id == run.saga_id)
            .values(next_attempt_at=saga_harness.clock.now + timedelta(days=1))
        )
    saga_harness.clock.advance(ENROLL.deadline + timedelta(seconds=1))
    assert await SagaSweeper(saga_harness.runtime).sweep_database("default") == 1


async def test_sweep_purges_terminal_instances_after_retention(
    saga_harness, monkeypatch: Any
) -> None:
    _ledger(saga_harness)
    run = await saga_harness.run(ENROLL, input=enroll_input())
    monkeypatch.setattr(saga_harness.core.settings, "saga_retention_days", 1)
    saga_harness.clock.advance(timedelta(days=2))
    sweeper = SagaSweeper(saga_harness.runtime)
    await sweeper.sweep_database("default", gauge=True, purge=True)
    with pytest.raises(AssertionError, match="no "):
        await saga_harness.instance(ENROLL, run.saga_id)


async def test_sweep_leaves_a_fresh_nudge_alone_until_an_interval_passed(
    saga_harness,
) -> None:
    """A just-started instance has its own nudge in flight: the sweep only
    re-drives it once it has not moved for a whole interval (nudge lost)."""
    _ledger(saga_harness)
    await saga_harness.start(ENROLL, saga_id="fresh-1", input=enroll_input())
    sweeper = SagaSweeper(saga_harness.runtime)
    assert await sweeper.sweep_database("default") == 0
    saga_harness.clock.advance(timedelta(seconds=6))
    assert await sweeper.sweep_database("default") == 1
