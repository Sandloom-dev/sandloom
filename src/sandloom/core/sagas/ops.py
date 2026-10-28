"""Operator commands on saga instances (ADR-0008 §8): retry, park, resolve.

Each one locks the instance row (waiting, not skipping), checks the rule,
changes the state with a version bump and writes its audit row in the SAME
transaction (``core.saga.retried`` / ``core.saga.parked`` /
``core.saga.step_resolved``) — through the cross-tenant session (the system
channel under ``tenant_rls``). A nudge is published afterwards, in a Unit of
Work of its own (the timer sweep re-drives the instance if that fails).

**Retry after the dedupe window.** ``retry`` refuses while the current step's
outcome is unknown (its last attempt was transient / ambiguous) and its
command-log record may have expired (first attempt older than
``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`` minus one sweep interval and the RPC
timeout): a re-call could run the step a second time. The operator
establishes the outcome at the provider (``sandloom sagas show`` prints the step's
operation key) and records it with ``resolve --as applied|skipped``. A known
outcome (a refusal, a step never attempted) retries as usual.

Every command is a dry run unless ``apply`` (the CLI's ``--apply --reason``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import sandloom.core.sql_alchemy.saga_store as _store
from sandloom.core.descriptors import BACKWARD, FORWARD, CommandStep, Saga, StepKind
from sandloom.core.sagas.channel import cross_tenant_session, write_audit
from sandloom.core.sagas.outcomes import Outcome
from sandloom.core.sagas.registry import RegisteredSaga
from sandloom.core.sagas.runtime import (
    SagaRuntime,
    compensation_window,
    next_applied_index,
    record_may_have_expired,
)
from sandloom.core.sql_alchemy.models.saga import ACTIVE_STATUSES
from sandloom.core.sql_alchemy.saga_store import InstanceRow
from sandloom.core.unit_of_work import db_session

Resolution = Literal["applied", "skipped"]


class SagaOperationError(RuntimeError):
    """The operator command cannot be applied (exit code 1, with a hint)."""


class _DryRun(Exception):
    """Rolls a dry run's transaction back (nothing is written)."""


@dataclass(slots=True)
class OperatorIntent:
    apply: bool
    operator: str
    reason: str


@dataclass(slots=True)
class OperationResult:
    action: str
    saga_type: str
    saga_id: str
    applied: bool
    before: str
    after: str
    message: str
    hints: list[str] = field(default_factory=list)


def registered_type(runtime: SagaRuntime, saga_type: str) -> RegisteredSaga:
    """The newest registered version of ``saga_type`` (its database/channel)."""
    found = [r for r in runtime.registry if r.saga_type == saga_type]
    if not found:
        known = sorted({r.saga_type for r in runtime.registry})
        raise SagaOperationError(
            f"saga type {saga_type!r} is not registered in this process "
            f"(known: {known}); run the command where its module is loaded"
        )
    return max(found, key=lambda r: r.version)


def current_call(
    definition: Saga[Any] | None, row: InstanceRow
) -> tuple[str | None, str]:
    """``(step name, direction)`` of the instance's current step call."""
    direction = FORWARD
    if row.direction == "backward" and not row.resolving:
        direction = BACKWARD
    if definition is None or not 0 <= row.step_index < len(definition.steps):
        return None, direction
    return definition.steps[row.step_index].name, direction


def outcome_unknown_after_ttl(
    definition: Saga[Any] | None, row: InstanceRow, now: Any, settings: Any
) -> bool:
    """The post-TTL rule (§8): unknown outcome + possibly expired record."""
    if definition is None or not 0 <= row.step_index < len(definition.steps):
        return False
    step = definition.steps[row.step_index]
    ambiguous = (
        isinstance(step, CommandStep)
        and row.attempt > 0
        and row.last_outcome == Outcome.TRANSIENT
    )
    return ambiguous and record_may_have_expired(
        row.step_first_attempt_at, now, settings
    )


async def _locked(cts_session: Any, reg: RegisteredSaga, saga_id: str) -> InstanceRow:
    found = await _store.find_instance(
        cts_session, reg.saga_type, saga_id, tenant=_store.ANY_TENANT
    )
    if found is None:
        raise SagaOperationError(f"no {reg.saga_type} instance {saga_id!r}")
    row = await _store.lock_instance(
        cts_session, found.id, tenant=_store.ANY_TENANT, skip_locked=False
    )
    if row is None:  # pragma: no cover - deleted between the two statements
        raise SagaOperationError(f"{reg.saga_type} {saga_id!r} disappeared")
    return row


async def _nudge(runtime: SagaRuntime, reg: RegisteredSaga, row: InstanceRow) -> None:
    async with db_session(reg.database):
        await runtime.publish_nudge(reg, row.id, row.version, row.tenant_id)


async def _run(
    runtime: SagaRuntime,
    saga_type: str,
    saga_id: str,
    intent: OperatorIntent,
    action: str,
    mutate: Any,
) -> OperationResult:
    """Lock, ``mutate(reg, definition, row) -> (message, audit data)``, audit."""
    reg = registered_type(runtime, saga_type)
    try:
        result, row = await _mutate_locked(
            runtime, reg, saga_id, intent, action, mutate
        )
    except _DryRun as dry:
        return dry.args[0]  # type: ignore[no-any-return]
    if row.status in ACTIVE_STATUSES:
        await _nudge(runtime, reg, row)
    return result


async def _mutate_locked(
    runtime: SagaRuntime,
    reg: RegisteredSaga,
    saga_id: str,
    intent: OperatorIntent,
    action: str,
    mutate: Any,
) -> tuple[OperationResult, InstanceRow]:
    async with cross_tenant_session(
        runtime.core,
        reg.database,
        actor=f"cli:{intent.operator or 'operator'}",
        reason=intent.reason or f"sandloom sagas {action} (dry run)",
        channel=runtime.system_channel,
    ) as cts:
        row = await _locked(cts.session, reg, saga_id)
        version_reg = runtime.registry.get(row.saga_type, row.definition_version)
        definition = version_reg.definition if version_reg is not None else None
        before = row.status
        message, data, hints = mutate(reg, definition, row)
        result = OperationResult(
            action,
            reg.saga_type,
            saga_id,
            intent.apply,
            before,
            row.status,
            message,
            hints,
        )
        if not intent.apply:
            raise _DryRun(result)
        row.version += 1
        row.updated_at = runtime.now()
        await _store.update_instance(cts.session, row)
        write_audit(
            cts.session,
            f"core.saga.{data.pop('_action')}",
            actor=intent.operator,
            resource_id=f"{row.saga_type}/{row.saga_id}",
            data={"reason": intent.reason, "version": row.version, **data},
        )
        await _record_resolution(cts.session, row, data)
    return result, row


async def _record_resolution(
    session: Any, row: InstanceRow, data: dict[str, Any]
) -> None:
    resolved = data.get("resolved")
    if resolved is None:
        return
    await _store.upsert_step(
        session,
        {
            "instance_id": row.id,
            "step": data["step"],
            "direction": data["direction"],
            "saga_type": row.saga_type,
            "tenant_id": row.tenant_id,
            "status": f"resolved_{resolved}",
            "outcome": None,
            "attempts": 0,
            "operation_key": None,
            "first_attempt_at": None,
            "last_attempt_at": row.updated_at,
            "last_duration_ms": None,
            "last_error": None,
        },
    )


# --------------------------------------------------------------------------- #
# retry
# --------------------------------------------------------------------------- #
async def retry(
    runtime: SagaRuntime, saga_type: str, saga_id: str, intent: OperatorIntent
) -> OperationResult:
    """Re-drive now: un-park to the parked direction, fresh window (§8)."""
    now = runtime.now()
    settings = runtime.settings

    def mutate(
        reg: RegisteredSaga, definition: Saga[Any] | None, row: InstanceRow
    ) -> tuple[str, dict[str, Any], list[str]]:
        if row.status not in (*ACTIVE_STATUSES, "parked"):
            raise SagaOperationError(
                f"{saga_type} {saga_id!r} is {row.status}: nothing to retry"
            )
        if definition is None:
            raise SagaOperationError(
                f"{saga_type} v{row.definition_version} is not registered here: deploy "
                "that version (alongside the new one) before retrying"
            )
        step, direction = current_call(definition, row)
        if outcome_unknown_after_ttl(definition, row, now, settings):
            raise SagaOperationError(
                f"refusing to retry {saga_type} {saga_id!r}: step {step!r} "
                f"({direction}) has an unknown outcome and its dedupe record may "
                "have expired — a re-call could run it twice.\n"
                f"  establish the outcome at the provider (operation key "
                f"saga:{row.saga_type}:{row.saga_id}:{step}:{FORWARD}), then: "
                f"sandloom sagas resolve {saga_type} {saga_id} --step {step} "
                "--as applied|skipped --apply --reason ..."
            )
        target = row.parked_from if row.status == "parked" else row.status
        if target not in ACTIVE_STATUSES:
            target = "running"
        row.status, row.park_reason, row.parked_from = str(target), None, None
        row.next_attempt_at = now
        row.swept_at = None
        if target == "running":
            row.deadline_at = now + definition.deadline
            row.step_deadline_at = None
        else:
            row.compensation_deadline_at = now + compensation_window(reg, settings)
        return (
            f"{target} from step {step!r} ({direction}) with a fresh deadline",
            {"_action": "retried", "step": step, "direction": direction},
            [],
        )

    return await _run(runtime, saga_type, saga_id, intent, "retry", mutate)


# --------------------------------------------------------------------------- #
# park
# --------------------------------------------------------------------------- #
async def park(
    runtime: SagaRuntime, saga_type: str, saga_id: str, intent: OperatorIntent
) -> OperationResult:
    """Stop an active instance (``park_reason=operator``)."""

    def mutate(
        reg: RegisteredSaga, definition: Saga[Any] | None, row: InstanceRow
    ) -> tuple[str, dict[str, Any], list[str]]:
        if row.status not in ACTIVE_STATUSES:
            raise SagaOperationError(
                f"{saga_type} {saga_id!r} is {row.status}: not active"
            )
        step, direction = current_call(definition, row)
        row.parked_from, row.status, row.park_reason = row.status, "parked", "operator"
        return (
            "parked (operator)",
            {
                "_action": "parked",
                "reason_code": "operator",
                "step": step,
                "direction": direction,
            },
            [
                (
                    f"resume: sandloom sagas retry {saga_type} {saga_id} "
                    f"--apply --reason ..."
                )
            ],
        )

    return await _run(runtime, saga_type, saga_id, intent, "park", mutate)


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
def _resolve_forward(
    definition: Saga[Any], row: InstanceRow, step: str, as_: Resolution
) -> str:
    if as_ == "applied":
        row.results.setdefault(step, None)
        if step not in row.applied:
            row.applied.append(step)
    _reset(row)
    row.step_index += 1
    if row.step_index >= len(definition.steps):
        row.status = "completed"
        return "completed"
    row.status = "running"
    return f"running from step {definition.steps[row.step_index].name!r}"


def _resolve_pending(
    definition: Saga[Any], row: InstanceRow, step: str, as_: Resolution
) -> str:
    """The ambiguous forward call of a compensating instance (resolve first)."""
    row.resolving = False
    current = definition.steps[row.step_index]
    if as_ == "applied":
        if step not in row.applied:
            row.applied.append(step)
        if current.kind != StepKind.COMPENSATABLE:  # the pivot did apply
            row.direction, row.compensation_deadline_at = "forward", None
            return _resolve_forward(definition, row, step, "applied")
        _reset(row)
        row.status = "compensating"
        return f"compensating {step!r} next"
    return _backward(definition, row, row.step_index - 1)


def _backward(definition: Saga[Any], row: InstanceRow, start: int) -> str:
    _reset(row)
    row.step_index = next_applied_index(definition, row.applied, start)
    if row.step_index < 0:
        row.status, row.step_index, row.compensation_deadline_at = (
            "compensated",
            -1,
            None,
        )
        return "compensated"
    row.status = "compensating"
    return f"compensating {definition.steps[row.step_index].name!r} next"


def _reset(row: InstanceRow) -> None:
    row.attempt = 0
    row.last_outcome = None
    row.last_error = None
    row.step_first_attempt_at = None
    row.step_deadline_at = None
    row.park_reason = None
    row.parked_from = None
    row.swept_at = None


def _apply_resolution(
    reg: RegisteredSaga,
    definition: Saga[Any],
    row: InstanceRow,
    step: str,
    as_: Resolution,
    now: Any,
    runtime: SagaRuntime,
) -> str:
    row.next_attempt_at = now
    if row.parked_from == "running":
        message = _resolve_forward(definition, row, step, as_)
    elif row.resolving:
        message = _resolve_pending(definition, row, step, as_)
    else:  # a compensation: applied (undone by hand) or skipped, same move
        if step in row.applied:
            row.applied.remove(step)
        message = _backward(definition, row, row.step_index - 1)
    if row.status == "compensating" and row.compensation_deadline_at is None:
        row.compensation_deadline_at = now + compensation_window(reg, runtime.settings)
    if row.status == "running":
        row.deadline_at = max(row.deadline_at, now + definition.deadline)
    return message


async def resolve(
    runtime: SagaRuntime,
    saga_type: str,
    saga_id: str,
    step: str,
    as_: Resolution,
    intent: OperatorIntent,
) -> OperationResult:
    """An operator asserts the current step's outcome, then the saga continues."""
    if as_ not in ("applied", "skipped"):
        raise SagaOperationError("--as must be applied or skipped")
    now = runtime.now()

    def mutate(
        reg: RegisteredSaga, definition: Saga[Any] | None, row: InstanceRow
    ) -> tuple[str, dict[str, Any], list[str]]:
        if row.status != "parked":
            raise SagaOperationError(
                f"{saga_type} {saga_id!r} is {row.status}: resolve applies to a "
                "parked instance (park it first)"
            )
        if definition is None:
            raise SagaOperationError(
                f"{saga_type} v{row.definition_version} is not registered here"
            )
        current, direction = current_call(definition, row)
        if current != step:
            raise SagaOperationError(
                f"step {step!r} is not the current step of {saga_type} {saga_id!r} "
                f"(current: {current!r}, {direction})"
            )
        message = _apply_resolution(reg, definition, row, step, as_, now, runtime)
        return (
            message,
            {
                "_action": "step_resolved",
                "step": step,
                "direction": direction,
                "resolved": as_,
            },
            [],
        )

    return await _run(runtime, saga_type, saga_id, intent, "resolve", mutate)


# --------------------------------------------------------------------------- #
# health (sandloom doctor / the startup report)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Attention:
    database: str
    parked: int
    overdue: int
    #: ``"<type> v<version>"`` of active instances no definition here covers.
    unknown: list[str]


async def attention(runtime: SagaRuntime, database: str, *, actor: str) -> Attention:
    """Parked, overdue and unknown-definition instances (read-only)."""
    found = Attention(database, 0, 0, [])
    async with cross_tenant_session(
        runtime.core,
        database,
        actor=actor,
        reason="saga health report (read-only)",
        channel=runtime.system_channel,
        read_only=True,
    ) as cts:
        counts = await _store.attention_counts(cts.session, runtime.now())
        found.parked, found.overdue = counts["parked"], counts["overdue"]
        for saga_type, version, count in await _store.active_definitions(cts.session):
            if runtime.registry.get(saga_type, version) is None:
                found.unknown.append(f"{saga_type} v{version} ({count})")
    return found


# --------------------------------------------------------------------------- #
# read side (list / show)
# --------------------------------------------------------------------------- #
async def list_instances(
    runtime: SagaRuntime, database: str, flt: _store.ListFilter, *, operator: str
) -> list[InstanceRow]:
    async with cross_tenant_session(
        runtime.core,
        database,
        actor=f"cli:{operator or 'operator'}",
        reason="sandloom sagas list",
        channel=runtime.system_channel,
    ) as cts:
        return await _store.list_instances(cts.session, flt)


async def show_instance(
    runtime: SagaRuntime, saga_type: str, saga_id: str, *, operator: str
) -> tuple[InstanceRow, list[dict[str, Any]]]:
    reg = registered_type(runtime, saga_type)
    async with cross_tenant_session(
        runtime.core,
        reg.database,
        actor=f"cli:{operator or 'operator'}",
        reason="sandloom sagas show",
        channel=runtime.system_channel,
    ) as cts:
        row = await _store.find_instance(
            cts.session, saga_type, saga_id, tenant=_store.ANY_TENANT
        )
        if row is None:
            raise SagaOperationError(f"no {saga_type} instance {saga_id!r}")
        return row, await _store.steps_of(cts.session, row.id)


__all__ = [
    "Attention",
    "OperationResult",
    "OperatorIntent",
    "Resolution",
    "SagaOperationError",
    "current_call",
    "list_instances",
    "outcome_unknown_after_ttl",
    "park",
    "registered_type",
    "resolve",
    "retry",
    "attention",
    "show_instance",
]
