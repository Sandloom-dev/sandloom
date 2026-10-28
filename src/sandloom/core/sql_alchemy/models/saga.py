"""Saga instance state (ADR-0008 §2): ``saga_instances`` + ``saga_steps``.

Core-chain tables in the database hosting the owning module (the same rule as
its outbox). An instance row is **version-guarded state**, not a dedupe
record: it answers "where is this process", never "did this effect already
run" — that is the provider's durable command log (ADR-0005 §4, row "saga
runtime"). ``saga_steps`` holds one row per ``(instance, step, direction)``
updated in place, for ``sandloom sagas show`` and metrics.

Both tables carry ``tenant_id`` and, under ``tenant_rls``, the tenant policy
with system access (``enable_tenant_rls(..., system_access=True)``): the
runtime touches an instance with its tenant bound, cross-tenant work (the
timer sweep, operator commands) goes through the system channel. ``input`` and
``results`` hold ids, never PII (the event-payload rule).

Times are naive UTC, like every core-chain table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.sql_alchemy.base import Base

#: Statuses an instance can be advanced from.
ACTIVE_STATUSES = ("running", "compensating")


class SagaInstance(Base):
    __tablename__ = "saga_instances"
    __table_args__ = (
        UniqueConstraint("saga_type", "saga_id", name="uq_saga_instances_type_id"),
        # The sweep's due scan: active rows only (terminal ones never match).
        Index(
            "ix_saga_instances_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('running', 'compensating')"),
        ),
        Index("ix_saga_instances_status_updated", "status", "updated_at"),
        Index("ix_saga_instances_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    #: ``<module>.<name>`` of the definition.
    saga_type: Mapped[str] = mapped_column(String(200), nullable=False)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The business id (unique per type): starting it again is a create-or-get.
    saga_id: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: running | compensating | completed | compensated | parked
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Index of the step to run next (forward) or to compensate next (backward).
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: forward | backward
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Backward pass only: the current step's last forward attempt was
    #: ambiguous — re-issue it (same key) before compensating (§7).
    resolving: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Attempts of the current step call (0 = not attempted yet).
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Outcome class of the current step's last attempt (outcomes table).
    last_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: First attempt of the current step call (post-TTL retry rule, §8).
    step_first_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    #: The current step's own deadline (``step(..., deadline=)``), if any.
    step_deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Set when compensation starts.
    compensation_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: step name -> JSON result of every applied forward step.
    results: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: Names of the forward steps whose effect exists (compensated backwards).
    applied: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    park_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Status to return to on ``sandloom sagas retry`` (running / compensating).
    parked_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Optimistic version: every transition bumps it; nudges carry it.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Trace of the request that started the saga (§14 trace linking).
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Request id of the start (``started … by``).
    started_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Last time the timer sweep nudged it (re-driven at most once/interval).
    swept_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SagaStepRecord(Base):
    __tablename__ = "saga_steps"
    __table_args__ = (Index("ix_saga_steps_tenant_id", "tenant_id"),)

    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    step: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: do | undo
    direction: Mapped[str] = mapped_column(String(4), primary_key=True)
    #: Copied from the instance: filters (extract-module, metrics) and RLS.
    saga_type: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: succeeded | failed | refused | resolved_applied | resolved_skipped
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Outcome class of the last attempt.
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operation_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
