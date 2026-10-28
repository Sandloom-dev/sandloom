"""Relational statements of the saga runtime (ADR-0008 §2, §5, §6).

The saga runtime (:mod:`sandloom.core.sagas`) never builds SQL itself: every
statement it runs against ``saga_instances`` / ``saga_steps`` lives here.

Per step the protocol costs four statements in the transition's own UoW:
:func:`lock_instance` (``FOR UPDATE SKIP LOCKED``), :func:`upsert_step`,
:func:`update_instance` and the outbox insert of the next nudge (not here —
``publish_event``). The start adds one :func:`insert_instance` to the caller's
transaction. The timer sweep runs :func:`claim_due` (``FOR UPDATE SKIP
LOCKED LIMIT n`` + the ``swept_at`` stamp).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import Table, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.sql_alchemy.models.saga import (
    ACTIVE_STATUSES,
    SagaInstance,
    SagaStepRecord,
)

INSTANCES = cast(Table, SagaInstance.__table__)
STEPS = cast(Table, SagaStepRecord.__table__)

#: Terminal statuses the retention purge removes (``parked`` never).
PURGEABLE_STATUSES = ("completed", "compensated")


class _Tenants(enum.Enum):
    ANY = "any"


#: The ``tenant`` of a cross-tenant read: only the system channel (the
#: sweep, operator commands, the test harness) passes it.
ANY_TENANT: Final = _Tenants.ANY
#: Whose instance a read may return: the bound tenant (``None``: only a
#: tenant-less saga) or :data:`ANY_TENANT`. The runtime never relies on RLS
#: alone: a database without ``tenant_rls`` gets the same isolation here
#: (G-SAGA-03).
TenantFilter = uuid.UUID | None | Literal[_Tenants.ANY]


def _of_tenant(tenant: TenantFilter) -> list[Any]:
    if tenant is ANY_TENANT:
        return []
    if tenant is None:
        return [INSTANCES.c.tenant_id.is_(None)]
    return [INSTANCES.c.tenant_id == tenant]


@dataclass(slots=True)
class InstanceRow:
    """A ``saga_instances`` row as plain, mutable data (one transition)."""

    id: uuid.UUID
    saga_type: str
    definition_version: int
    saga_id: str
    tenant_id: uuid.UUID | None
    status: str
    step_index: int
    direction: str
    resolving: bool
    attempt: int
    last_outcome: str | None
    step_first_attempt_at: datetime | None
    next_attempt_at: datetime
    deadline_at: datetime
    step_deadline_at: datetime | None
    compensation_deadline_at: datetime | None
    input: dict[str, Any]
    results: dict[str, Any]
    applied: list[str]
    last_error: dict[str, Any] | None
    park_reason: str | None
    parked_from: str | None
    version: int
    trace_id: str | None
    started_by: str | None
    swept_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_mapping(cls, row: Mapping[Any, Any]) -> InstanceRow:
        values = {f.name: row[f.name] for f in fields(cls)}
        values["results"] = dict(values["results"] or {})
        values["applied"] = list(values["applied"] or [])
        values["input"] = dict(values["input"] or {})
        return cls(**values)

    def as_values(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class DueInstance:
    """What the sweep needs to nudge one instance."""

    id: uuid.UUID
    saga_type: str
    tenant_id: uuid.UUID | None
    version: int


async def insert_instance(session: AsyncSession, values: Mapping[str, Any]) -> bool:
    """INSERT the instance unless ``(saga_type, saga_id)`` exists (create-or-get).

    ``True`` when this call created it. Runs in the CALLER's transaction: the
    unique index makes a concurrent start of the same business id wait for the
    other transaction, then see its row.
    """
    stmt = (
        pg_insert(INSTANCES)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["saga_type", "saga_id"])
        .returning(INSTANCES.c.id)
    )
    created = (await session.execute(stmt)).first()
    return created is not None


async def find_instance(
    session: AsyncSession, saga_type: str, saga_id: str, *, tenant: TenantFilter
) -> InstanceRow | None:
    """The instance of ``(saga_type, saga_id)`` when it belongs to ``tenant``."""
    stmt = select(INSTANCES).where(
        INSTANCES.c.saga_type == saga_type,
        INSTANCES.c.saga_id == saga_id,
        *_of_tenant(tenant),
    )
    row = (await session.execute(stmt)).mappings().first()
    return InstanceRow.from_mapping(row) if row is not None else None


async def lock_instance(
    session: AsyncSession,
    instance_id: uuid.UUID,
    *,
    tenant: TenantFilter,
    skip_locked: bool = True,
) -> InstanceRow | None:
    """Lock the instance row for one transition.

    ``skip_locked`` (the advance path): another worker holding the row means
    it owns this transition — ``None``, and the nudge is acknowledged as a
    no-op. Operator commands wait instead (bounded by the session's
    ``lock_timeout``). An instance of another ``tenant`` is ``None`` too.
    """
    stmt = (
        select(INSTANCES)
        .where(INSTANCES.c.id == instance_id, *_of_tenant(tenant))
        .with_for_update(skip_locked=skip_locked)
    )
    row = (await session.execute(stmt)).mappings().first()
    return InstanceRow.from_mapping(row) if row is not None else None


async def update_instance(session: AsyncSession, row: InstanceRow) -> None:
    """Write the (locked) row back; the caller already bumped ``version``."""
    values = row.as_values()
    values.pop("id")
    await session.execute(
        update(INSTANCES).where(INSTANCES.c.id == row.id).values(**values)
    )


async def upsert_step(session: AsyncSession, values: Mapping[str, Any]) -> None:
    """Insert or update the ``(instance, step, direction)`` row in place.

    ``first_attempt_at`` keeps its first value; ``attempts`` is the caller's
    running count (the instance row is locked, so no concurrent writer).
    """
    stmt = pg_insert(STEPS).values(**values)
    updated: dict[str, Any] = {
        key: stmt.excluded[key]
        for key in values
        if key not in ("instance_id", "step", "direction", "first_attempt_at")
    }
    updated["first_attempt_at"] = func.coalesce(
        STEPS.c.first_attempt_at, stmt.excluded.first_attempt_at
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["instance_id", "step", "direction"], set_=updated
        )
    )


async def mark_step_started(
    session: AsyncSession, values: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Insert the ``(instance, step, direction)`` row as ``started`` unless it
    exists (the durable "being called" marker of a forward command step).

    ``None`` when this call inserted it; otherwise the existing row's
    ``status``, ``outcome`` and ``first_attempt_at`` (a surviving marker of
    a call whose transition rolled back, or a recorded attempt)."""
    inserted = await session.execute(
        pg_insert(STEPS)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["instance_id", "step", "direction"])
        .returning(STEPS.c.step)
    )
    if inserted.first() is not None:
        return None
    found = await session.execute(
        select(STEPS.c.status, STEPS.c.outcome, STEPS.c.first_attempt_at)
        .where(STEPS.c.instance_id == values["instance_id"])
        .where(STEPS.c.step == values["step"])
        .where(STEPS.c.direction == values["direction"])
        .limit(1)
    )
    row = found.mappings().first()
    return None if row is None else dict(row)


async def step_started(
    session: AsyncSession, instance_id: uuid.UUID, step: str, direction: str
) -> bool:
    """Whether the step was ever called (a marker or a recorded attempt)."""
    found = await session.execute(
        select(STEPS.c.step)
        .where(STEPS.c.instance_id == instance_id)
        .where(STEPS.c.step == step)
        .where(STEPS.c.direction == direction)
        .limit(1)
    )
    return found.first() is not None


def _due_clause(now: datetime, swept_before: datetime) -> Any:
    """Active instances the sweep must nudge now.

    A deadline that passed always counts. ``next_attempt_at`` counts when a
    backoff elapsed (``next_attempt_at > updated_at``) or when the instance
    has not moved for a whole interval (its own nudge was lost): an instance
    whose transition just published its next nudge (``next_attempt_at ==
    updated_at``) is left to that nudge.
    """
    retry_due = and_(
        INSTANCES.c.next_attempt_at <= now,
        or_(
            INSTANCES.c.next_attempt_at > INSTANCES.c.updated_at,
            INSTANCES.c.updated_at <= swept_before,
        ),
    )
    running = and_(
        INSTANCES.c.status == "running",
        or_(
            retry_due,
            INSTANCES.c.deadline_at <= now,
            INSTANCES.c.step_deadline_at <= now,
        ),
    )
    compensating = and_(
        INSTANCES.c.status == "compensating",
        or_(retry_due, INSTANCES.c.compensation_deadline_at <= now),
    )
    return or_(running, compensating)


async def claim_due(
    session: AsyncSession, *, now: datetime, swept_before: datetime, limit: int
) -> list[DueInstance]:
    """The due active instances not nudged since ``swept_before``, stamped now.

    ``FOR UPDATE SKIP LOCKED``: an instance whose step is running right now is
    skipped (its own transition publishes the next nudge, or the next tick
    finds it again).
    """
    stmt = (
        select(
            INSTANCES.c.id,
            INSTANCES.c.saga_type,
            INSTANCES.c.tenant_id,
            INSTANCES.c.version,
        )
        .where(
            _due_clause(now, swept_before),
            or_(INSTANCES.c.swept_at.is_(None), INSTANCES.c.swept_at <= swept_before),
        )
        .order_by(INSTANCES.c.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).all()
    due = [DueInstance(r.id, r.saga_type, r.tenant_id, r.version) for r in rows]
    if due:
        await session.execute(
            update(INSTANCES)
            .where(INSTANCES.c.id.in_([d.id for d in due]))
            .values(swept_at=now)
        )
    return due


async def count_by_status(session: AsyncSession) -> list[tuple[str, str, int]]:
    """``(saga_type, status, count)`` — the ``sandloom_saga_instances`` gauge."""
    stmt = select(INSTANCES.c.saga_type, INSTANCES.c.status, func.count()).group_by(
        INSTANCES.c.saga_type, INSTANCES.c.status
    )
    return [(str(t), str(s), int(n)) for t, s, n in (await session.execute(stmt)).all()]


async def attention_counts(session: AsyncSession, now: datetime) -> dict[str, int]:
    """Parked and overdue (active past their deadline) instances
    (``sandloom doctor``).
    """
    parked = select(func.count()).where(INSTANCES.c.status == "parked")
    overdue = select(func.count()).where(
        INSTANCES.c.status.in_(ACTIVE_STATUSES), INSTANCES.c.deadline_at <= now
    )
    return {
        "parked": int((await session.execute(parked)).scalar_one()),
        "overdue": int((await session.execute(overdue)).scalar_one()),
    }


async def active_definitions(session: AsyncSession) -> list[tuple[str, int, int]]:
    """``(saga_type, definition_version, count)`` of the active instances."""
    stmt = (
        select(INSTANCES.c.saga_type, INSTANCES.c.definition_version, func.count())
        .where(INSTANCES.c.status.in_(ACTIVE_STATUSES))
        .group_by(INSTANCES.c.saga_type, INSTANCES.c.definition_version)
    )
    rows = (await session.execute(stmt)).all()
    return [(str(t), int(v), int(n)) for t, v, n in rows]


@dataclass(frozen=True, slots=True)
class ListFilter:
    saga_type: str | None = None
    status: Sequence[str] = ()
    version: int | None = None
    tenant_id: uuid.UUID | None = None
    created_before: datetime | None = None
    #: The actionable view: parked, compensating, or active past the deadline.
    actionable_at: datetime | None = None
    limit: int = 100


async def list_instances(session: AsyncSession, flt: ListFilter) -> list[InstanceRow]:
    stmt = select(INSTANCES)
    if flt.saga_type:
        stmt = stmt.where(INSTANCES.c.saga_type == flt.saga_type)
    if flt.status:
        stmt = stmt.where(INSTANCES.c.status.in_(list(flt.status)))
    if flt.version is not None:
        stmt = stmt.where(INSTANCES.c.definition_version == flt.version)
    if flt.tenant_id is not None:
        stmt = stmt.where(INSTANCES.c.tenant_id == flt.tenant_id)
    if flt.created_before is not None:
        stmt = stmt.where(INSTANCES.c.created_at <= flt.created_before)
    if flt.actionable_at is not None:
        stmt = stmt.where(
            or_(
                INSTANCES.c.status.in_(("parked", "compensating")),
                and_(
                    INSTANCES.c.status == "running",
                    INSTANCES.c.deadline_at <= flt.actionable_at,
                ),
            )
        )
    stmt = stmt.order_by(INSTANCES.c.created_at).limit(flt.limit)
    rows = (await session.execute(stmt)).mappings().all()
    return [InstanceRow.from_mapping(r) for r in rows]


async def steps_of(
    session: AsyncSession, instance_id: uuid.UUID
) -> list[dict[str, Any]]:
    stmt = (
        select(STEPS)
        .where(STEPS.c.instance_id == instance_id)
        .order_by(STEPS.c.first_attempt_at.nulls_last(), STEPS.c.direction)
    )
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]


async def purge_terminal(session: AsyncSession, *, before: datetime) -> int:
    """Delete completed / compensated instances (and their steps) last updated
    before ``before``. Parked instances are kept for an operator."""
    ids = (
        select(INSTANCES.c.id)
        .where(
            INSTANCES.c.status.in_(PURGEABLE_STATUSES),
            INSTANCES.c.updated_at < before,
        )
        .scalar_subquery()
    )
    await session.execute(delete(STEPS).where(STEPS.c.instance_id.in_(ids)))
    result = await session.execute(
        delete(INSTANCES).where(
            INSTANCES.c.status.in_(PURGEABLE_STATUSES),
            INSTANCES.c.updated_at < before,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "ANY_TENANT",
    "INSTANCES",
    "active_definitions",
    "PURGEABLE_STATUSES",
    "STEPS",
    "DueInstance",
    "TenantFilter",
    "InstanceRow",
    "ListFilter",
    "attention_counts",
    "claim_due",
    "count_by_status",
    "find_instance",
    "insert_instance",
    "list_instances",
    "lock_instance",
    "purge_terminal",
    "steps_of",
    "update_instance",
    "upsert_step",
]
