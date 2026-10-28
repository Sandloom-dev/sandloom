"""The saga step protocol (ADR-0008 §4, §6) and recovery rules (§7).

One transition per advance, in the advance consumer's own Unit of Work on the
owning module's database::

    row ← SELECT … FOR UPDATE SKIP LOCKED     # none → another worker owns it
    row terminal / version ≠ nudge's → no-op   # duplicate / stale / reordered
    deadline due → timeout transition          # compensate, or park
    next_attempt_at > now → no-op              # early (backoff not elapsed)
    run the current step (command with its derived key | local fn in a savepoint)
    apply the outcome (outcomes table): results, step_index ± 1, attempt,
        next_attempt_at, status, version + 1; upsert saga_steps
    publish <module>.saga_advance.v1 (if more to do now)   # outbox
    commit

The instance row lock is held across ONE step call (bounded by the RPC
timeout or the contract pool's ``lock_timeout``); the call itself runs on the
contract pool or in another process, never on this connection, and the
provider never touches saga tables. Everything that can move an instance —
deliveries, the sweep, operator commands — serializes on that lock and the
version.

Guarantees come from existing primitives (ADR-0005): command steps are
exactly-once-effect at the provider (durable command log, derived key), local
steps are transactional with the transition, progress is at-least-once
(outbox nudge + timer sweep). The instance row is version-guarded state, not a
dedupe record.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

import sandloom.core.observability.metrics as _metrics
import sandloom.core.observability.request_context as _request_context
import sandloom.core.observability.tracing as _tracing
import sandloom.core.sql_alchemy.saga_store as _store
from sandloom.core.descriptors import (
    BACKWARD,
    FORWARD,
    CommandStep,
    Direction,
    LocalStep,
    Saga,
    SagaContext,
    SagaDefinitionError,
    SagaStep,
    StepKind,
    operation_key,
    saga_step_key,
)
from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.sagas.outcomes import (
    SUCCESS,
    Outcome,
    StepOutcome,
    classify_exception,
)
from sandloom.core.sagas.registry import RegisteredSaga, SagaRegistry
from sandloom.core.sql_alchemy.models.saga import ACTIVE_STATUSES
from sandloom.core.sql_alchemy.saga_store import InstanceRow
from sandloom.core.tenant_context import bind_tenant, current_tenant_id_optional
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import (
    UnitOfWorkRequiredError,
    publish_event,
    record_audit,
)
from sandloom.core.unit_of_work.scope import _resolve_scoped_session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from sandloom.core.sagas.channel import SystemChannel

logger = logging.getLogger("sandloom.sagas")

#: Results of :meth:`SagaRuntime.advance` (the harness and tests read them).
NOOP_LOCKED = "noop:locked"
NOOP_STALE = "noop:stale"
NOOP_EARLY = "noop:early"
ADVANCED = "advanced"
RETRY_SCHEDULED = "retry"
PARKED = "parked"

_SYSTEM_RANDOM = secrets.SystemRandom()


def utcnow() -> datetime:
    """Naive UTC now (the core-chain tables store naive UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


class SagaInputConflict(RuntimeError):
    """``start_saga`` of an existing business id with OTHER input (409)."""

    http_status = 409
    code = "saga_input_conflict"


class SagaNotInstalled(RuntimeError):
    """No saga runtime on the active core (sagas are wired by ``create_app``)."""


@dataclass(frozen=True, slots=True)
class SagaHandle:
    """What ``start_saga`` / ``saga_status`` return."""

    saga_type: str
    saga_id: str
    instance_id: uuid.UUID
    status: str
    created: bool = False
    #: The current step (``None`` once terminal).
    step: str | None = None
    park_reason: str | None = None
    version: int = 0


@dataclass(slots=True)
class StepCall:
    """One step call, as the interceptor (test seam) and the logs see it."""

    saga_type: str
    saga_id: str
    instance_id: uuid.UUID
    step: str
    direction: Direction
    key: str
    attempt: int
    #: ``None`` for a local step.
    contract: str | None = None
    method: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.step}:{self.direction}"


#: ``async (call, proceed) -> result``: wraps every step call (test seam).
Interceptor = Callable[[StepCall, Callable[[], Awaitable[Any]]], Awaitable[Any]]
#: Called after a step call returned and BEFORE the transition is written
#: (test seam: raising here simulates a crash after the provider committed).
AfterCall = Callable[[StepCall], None]


def _handle(
    row: InstanceRow, definition: Saga[Any] | None, *, created: bool
) -> SagaHandle:
    step: str | None = None
    if (
        definition is not None
        and row.status in (*ACTIVE_STATUSES, "parked")
        and 0 <= row.step_index < len(definition.steps)
    ):
        step = definition.steps[row.step_index].name
    return SagaHandle(
        saga_type=row.saga_type,
        saga_id=row.saga_id,
        instance_id=row.id,
        status=row.status,
        created=created,
        step=step,
        park_reason=row.park_reason,
        version=row.version,
    )


def _check_saga_id(saga_id: object) -> str:
    text = str(saga_id)
    # The id is part of every derived key: same rule as operation_key.
    operation_key(text)
    if len(text) > 200:
        raise ValueError("saga_id must be at most 200 characters")
    return text


def _tenant_scope(tenant_id: uuid.UUID | None) -> AbstractContextManager[Any]:
    return bind_tenant(tenant_id) if tenant_id is not None else nullcontext()


class SagaRuntime:
    """The saga engine of one process (one per composition root).

    ``clock`` (naive UTC ``datetime``), ``random`` (``[0, 1)``, the jitter) and
    the ``interceptor`` / ``after_call`` seams are injectable — the
    ``saga_harness`` drives the real protocol through them.
    """

    def __init__(
        self,
        core: Any,
        registry: SagaRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        random: Callable[[], float] | None = None,
        system_channel: SystemChannel | None = None,
    ) -> None:
        self.core = core
        self.registry = registry
        #: The kernel system channel (cross-tenant work under tenant_rls).
        self.system_channel = system_channel
        self.clock: Callable[[], datetime] = clock or utcnow
        self.random: Callable[[], float] = random or _SYSTEM_RANDOM.random
        self.interceptor: Interceptor | None = None
        self.after_call: AfterCall | None = None

    @property
    def settings(self) -> Any:
        return self.core.settings

    def now(self) -> datetime:
        return self.clock()

    # ------------------------------------------------------------------ #
    # Start / status (the module-facing side, sandloom.core.di)
    # ------------------------------------------------------------------ #
    async def start(
        self,
        definition: Saga[Any],
        *,
        saga_id: object,
        input: BaseModel | Mapping[str, Any],
    ) -> SagaHandle:
        reg = self.registry.of(definition)
        session = _caller_session(reg.database)
        sid = _check_saga_id(saga_id)
        model = (
            input
            if isinstance(input, definition.input_model)
            else definition.input_model.model_validate(input)
        )
        payload = model.model_dump(mode="json")
        tenant = current_tenant_id_optional()
        now = self.now()
        values = _new_instance(reg, sid, tenant, payload, now)
        if await _store.insert_instance(session, values):
            await self.publish_nudge(reg, values["id"], 0, tenant)
            _metrics.saga_started_total.labels(reg.saga_type).inc()
            logger.info(
                "saga started",
                extra={"saga_type": reg.saga_type, "saga_id": sid},
            )
            return SagaHandle(
                saga_type=reg.saga_type,
                saga_id=sid,
                instance_id=values["id"],
                status="running",
                created=True,
                step=definition.steps[0].name,
            )
        existing = await _store.find_instance(
            session, reg.saga_type, sid, tenant=tenant
        )
        if (
            existing is None
            or existing.input != payload
            or existing.definition_version != definition.version
            or existing.tenant_id != tenant
        ):
            raise SagaInputConflict(
                f"saga {reg.saga_type} {sid!r} already exists with other input "
                "(or another version / tenant): a business id starts one saga"
            )
        return _handle(existing, definition, created=False)

    async def status(self, definition: Saga[Any], saga_id: object) -> SagaHandle | None:
        reg = self.registry.of(definition)
        sid = str(saga_id)
        # The bound tenant's instance only, with or without tenant RLS: a
        # business id is global, its state is not (G-SAGA-03). The tenant is
        # read once the unit is open: a unit of a single-tenant deployment
        # binds SINGLE_TENANT_ID when it opens.
        try:
            session = _resolve_scoped_session(reg.database, op="saga_status")
        except RuntimeError:
            session = None
        if session is not None:
            row = await _store.find_instance(
                session, reg.saga_type, sid, tenant=current_tenant_id_optional()
            )
        else:
            async with db_session(reg.database) as own:
                row = await _store.find_instance(
                    own, reg.saga_type, sid, tenant=current_tenant_id_optional()
                )
        return _handle(row, definition, created=False) if row is not None else None

    async def publish_nudge(
        self,
        reg: RegisteredSaga,
        instance_id: uuid.UUID,
        version: int,
        tenant_id: uuid.UUID | None,
    ) -> None:
        """An advance nudge through the outbox of the current UoW."""
        await publish_event(
            reg.channel,
            {
                "instance_id": str(instance_id),
                "expected_version": int(version),
                "tenant_id": str(tenant_id) if tenant_id is not None else None,
            },
            database=reg.database,
        )

    # ------------------------------------------------------------------ #
    # Advance (the kernel-installed consumer)
    # ------------------------------------------------------------------ #
    async def handle_nudge(
        self, session: AsyncSession, envelope: Mapping[str, Any]
    ) -> None:
        """The ``<module>:sagas@advance`` handler (``durable=False``)."""
        await self.advance_nudge(session, envelope)

    async def advance_nudge(
        self, session: AsyncSession, envelope: Mapping[str, Any]
    ) -> str:
        """Parse a nudge, bind its tenant, run :meth:`advance`."""
        instance_id, expected, tenant = parse_nudge(envelope)
        with _tenant_scope(tenant):
            return await self.advance(session, instance_id, expected, tenant=tenant)

    async def advance(
        self,
        session: AsyncSession,
        instance_id: uuid.UUID,
        expected_version: int,
        *,
        tenant: _store.TenantFilter,
    ) -> str:
        """Run at most one transition of ``instance_id`` (see the module doc).

        ``tenant`` (a nudge's): an instance of another tenant is never moved
        (the nudge is a no-op, as under tenant RLS)."""
        row = await _store.lock_instance(session, instance_id, tenant=tenant)
        if row is None:
            return NOOP_LOCKED
        if row.status not in ACTIVE_STATUSES or row.version != expected_version:
            return NOOP_STALE
        reg = self.registry.get(row.saga_type, row.definition_version)
        with _tenant_scope(row.tenant_id):
            if reg is None:
                return await _park_unknown(session, row, self.now())
            return await _Transition(self, session, reg, row, self.now()).run()


def parse_nudge(envelope: Mapping[str, Any]) -> tuple[uuid.UUID, int, uuid.UUID | None]:
    payload = envelope.get("payload")
    try:
        if not isinstance(payload, Mapping):
            raise TypeError("nudge payload is not an object")
        instance_id = uuid.UUID(str(payload["instance_id"]))
        expected = int(payload["expected_version"])
        raw_tenant = payload.get("tenant_id")
        tenant = uuid.UUID(str(raw_tenant)) if raw_tenant else None
    except (KeyError, TypeError, ValueError) as exc:
        raise PoisonEventError(f"malformed saga nudge {envelope.get('id')!r}") from exc
    return instance_id, expected, tenant


def _caller_session(database: str) -> AsyncSession:
    """The caller's UoW session on the saga's database (``start_saga``)."""
    try:
        anywhere = _resolve_scoped_session(None, op="start_saga")
    except RuntimeError:  # several databases active: one of them is the UoW
        anywhere = None
    else:
        if anywhere is None:
            raise UnitOfWorkRequiredError(
                "start_saga() called outside a Unit of Work: the instance row and "
                "its first nudge must commit with the business change. Fix: call "
                "it inside a UoW (a request with DbSession, `async with "
                "db_session():`, an event consumer)"
            )
    session = _resolve_scoped_session(database, op="start_saga")
    if session is None:  # pragma: no cover - _resolve_scoped_session raises
        raise UnitOfWorkRequiredError("start_saga() needs a Unit of Work")
    return session


def _new_instance(
    reg: RegisteredSaga,
    saga_id: str,
    tenant: uuid.UUID | None,
    payload: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    started_by = _request_context.request_id_var.get()
    return {
        "id": uuid.uuid4(),
        "saga_type": reg.saga_type,
        "definition_version": reg.version,
        "saga_id": saga_id,
        "tenant_id": tenant,
        "status": "running",
        "step_index": 0,
        "direction": "forward",
        "resolving": False,
        "attempt": 0,
        "last_outcome": None,
        "step_first_attempt_at": None,
        "next_attempt_at": now,
        "deadline_at": now + reg.definition.deadline,
        "step_deadline_at": None,
        "compensation_deadline_at": None,
        "input": payload,
        "results": {},
        "applied": [],
        "last_error": None,
        "park_reason": None,
        "parked_from": None,
        "version": 0,
        "trace_id": _tracing.get_current_trace_id(),
        "started_by": f"request {started_by}" if started_by else None,
        "swept_at": None,
        "created_at": now,
        "updated_at": now,
    }


def compensation_window(reg: RegisteredSaga, settings: Any) -> timedelta:
    explicit = reg.definition.compensation_deadline
    if explicit is not None:
        return explicit
    return timedelta(seconds=int(settings.saga_compensation_deadline_seconds))


def ttl_margin_seconds(settings: Any) -> float:
    """Safety margin of the post-TTL rule: one sweep interval + the RPC
    timeout and its single transport retry (§8)."""
    return float(settings.saga_sweep_interval_seconds) + 2.0 * float(
        settings.service_rpc_timeout_seconds
    )


def record_may_have_expired(
    first_attempt_at: datetime | None, now: datetime, settings: Any
) -> bool:
    """The step's command-log record may be gone: a re-call could run it again."""
    if first_attempt_at is None:
        return False
    ttl = float(settings.contract_command_dedupe_ttl_seconds)
    age = (now - first_attempt_at).total_seconds()
    return age >= ttl - ttl_margin_seconds(settings)


def next_applied_index(definition: Saga[Any], applied: list[str], start: int) -> int:
    """The highest index ``<= start`` whose step is applied and compensatable."""
    index = start
    while index >= 0:
        item = definition.steps[index]
        if item.name in applied and item.kind == StepKind.COMPENSATABLE:
            return index
        index -= 1
    return -1


class _Transition:
    """One locked transition of one instance (keeps each rule a small method)."""

    def __init__(
        self,
        runtime: SagaRuntime,
        session: AsyncSession,
        reg: RegisteredSaga,
        row: InstanceRow,
        now: datetime,
    ) -> None:
        self.runtime = runtime
        self.session = session
        self.reg = reg
        self.row = row
        self.now = now

    @property
    def definition(self) -> Saga[Any]:
        return self.reg.definition

    @property
    def settings(self) -> Any:
        return self.runtime.settings

    # -- entry --------------------------------------------------------------
    async def run(self) -> str:
        if self._deadline_due():
            return await self._on_deadline()
        if self.row.next_attempt_at > self.now:
            return NOOP_EARLY
        return await self._run_step()

    def _deadline_due(self) -> bool:
        row, now = self.row, self.now
        if row.status == "running":
            step_due = row.step_deadline_at is not None and row.step_deadline_at <= now
            return row.deadline_at <= now or step_due
        due = row.compensation_deadline_at
        return due is not None and due <= now

    def _past_pivot(self) -> bool:
        pivot = self.definition.pivot_index
        return pivot is not None and self.row.step_index > pivot

    def _current(self) -> SagaStep:
        return self.definition.steps[self.row.step_index]

    def _direction(self) -> Direction:
        row = self.row
        return FORWARD if row.direction == "forward" or row.resolving else BACKWARD

    def _ambiguous(self) -> bool:
        """The current step is a forward command step (only those can apply
        at a provider outside this transition)."""
        row = self.row
        return row.direction == "forward" and isinstance(self._current(), CommandStep)

    async def _may_have_applied(self) -> bool:
        """The current forward command step may have applied at its provider:
        a counted attempt, or the durable "started" marker its first call
        wrote in its own transaction (a crash after the provider committed
        rolls back the attempt count and the step record, never the marker)."""
        if not self._ambiguous():
            return False
        row = self.row
        if row.attempt > 0:
            return True
        return await _store.step_started(
            self.session, row.id, self._current().name, FORWARD
        )

    async def _on_deadline(self) -> str:
        if self.row.status == "compensating":
            return await self.park("compensation_deadline_exceeded")
        if self._past_pivot():
            return await self.park("deadline_exceeded")
        # Resolve before compensating (§7): re-issue the step with the SAME
        # key when it may have applied; the durable command log replays it.
        return await self.begin_compensation(
            resolve_current=await self._may_have_applied()
        )

    # -- running the step -------------------------------------------------
    async def _run_step(self) -> str:
        row = self.row
        step = self._current()
        direction = self._direction()
        if row.attempt == 0 and direction == FORWARD and isinstance(step, CommandStep):
            await self._claim_start(step)
        if (
            isinstance(step, CommandStep)
            and row.attempt > 0
            and row.last_outcome == Outcome.TRANSIENT
            and record_may_have_expired(
                row.step_first_attempt_at, self.now, self.settings
            )
        ):
            return await self.park("outcome_unknown_after_ttl")
        if row.attempt == 0:
            row.step_first_attempt_at = self.now
            if direction == FORWARD and step.deadline is not None:
                row.step_deadline_at = self.now + step.deadline
        row.attempt += 1
        call = self._step_call(step, direction)
        started = time.perf_counter()
        outcome, result = await self._invoke(step, direction, call)
        elapsed = time.perf_counter() - started
        if self.runtime.after_call is not None:
            self.runtime.after_call(call)
        self._observe(call, outcome, elapsed)
        await self._record_step(call, outcome, elapsed)
        return await self._apply(step, direction, outcome, result)

    async def _claim_start(self, step: CommandStep) -> None:
        """Durably record, BEFORE the provider can commit, that this forward
        command step is being called (its own short transaction): a crash
        after the provider committed rolls this transition back, and only
        this marker then tells the deadline path to resolve first (§7).

        A marker that already exists with an unknown outcome (``started``: a
        call whose transition rolled back; or a transient attempt) is that
        call: it is adopted as a counted transient attempt from the marker's
        own time, so the post-TTL guard parks it instead of re-issuing the
        command once the dedupe record may be gone (G-SAGA-12)."""
        row = self.row
        async with db_session(self.reg.database) as marker:
            existing = await _store.mark_step_started(
                marker,
                {
                    "instance_id": row.id,
                    "step": step.name,
                    "direction": FORWARD,
                    "saga_type": row.saga_type,
                    "tenant_id": row.tenant_id,
                    "status": "started",
                    "attempts": 0,
                    "operation_key": saga_step_key(
                        row.saga_type, row.saga_id, step.name, FORWARD
                    ),
                    "first_attempt_at": self.now,
                },
            )
        if existing is None:
            return
        unknown = existing["status"] == "started" or (
            existing["outcome"] == Outcome.TRANSIENT
        )
        first = existing["first_attempt_at"]
        if not unknown or first is None:
            return
        row.attempt = 1
        row.last_outcome = Outcome.TRANSIENT
        row.step_first_attempt_at = first
        if step.deadline is not None and row.step_deadline_at is None:
            row.step_deadline_at = first + step.deadline

    def _step_call(self, step: SagaStep, direction: Direction) -> StepCall:
        row = self.row
        call = StepCall(
            saga_type=row.saga_type,
            saga_id=row.saga_id,
            instance_id=row.id,
            step=step.name,
            direction=direction,
            key=saga_step_key(row.saga_type, row.saga_id, step.name, direction),
            attempt=row.attempt,
        )
        if isinstance(step, CommandStep):
            target = step.call if direction == FORWARD else step.compensate
            call.contract, call.method = str(target).rsplit(".", 1)
        return call

    def _context(self) -> SagaContext[Any]:
        row = self.row
        return SagaContext(
            saga_type=row.saga_type,
            saga_id=row.saga_id,
            input=self.reg.parse_input(row.input),
            results=dict(row.results),
        )

    async def _invoke(
        self, step: SagaStep, direction: Direction, call: StepCall
    ) -> tuple[StepOutcome, Any]:
        try:
            proceed = self._prepare(step, direction, call)
        except Exception as exc:
            logger.exception("saga step arguments failed", extra=self._log_fields(call))
            return StepOutcome(
                Outcome.MISCONFIGURED,
                None,
                "saga_args_failed",
                str(exc),
                type(exc).__name__,
            ), None
        refuse_on = step.refuse_on if isinstance(step, CommandStep) else frozenset()
        interceptor = self.runtime.interceptor
        with _tracing.start_span(f"saga.step {call.saga_type}.{call.label}") as span:
            _annotate(span, self.row, call)
            try:
                if interceptor is not None:
                    result = await interceptor(call, proceed)
                else:
                    result = await proceed()
            except Exception as exc:
                outcome = classify_exception(exc, refuse_on=refuse_on)
                # A non-HTTP failure (transport error, bug) keeps its traceback.
                logger.warning(
                    "saga step failed: %s (%s)",
                    outcome.outcome,
                    type(exc).__name__,
                    extra=self._failure_fields(call, outcome),
                    exc_info=outcome.status is None,
                )
                return outcome, None
        return SUCCESS, result

    def _prepare(
        self, step: SagaStep, direction: Direction, call: StepCall
    ) -> Callable[[], Awaitable[Any]]:
        """The zero-argument coroutine factory that performs the call."""
        ctx = self._context()
        session = self.session
        if isinstance(step, LocalStep):
            fn = step.apply if direction == FORWARD else step.compensate
            if fn is None:  # pragma: no cover - the DSL refuses it
                raise SagaDefinitionError(f"step {step.name!r} has no compensation")
            local_fn = fn

            async def run_local() -> Any:
                async with session.begin_nested():
                    return await local_fn(session, ctx)

            return run_local
        args_fn = step.args if direction == FORWARD else step.undo_args()
        kwargs = dict(args_fn(ctx))
        kwargs[self.reg.key_params[(step.name, direction)]] = call.key
        call.kwargs = kwargs
        core = self.runtime.core
        contract, method = str(call.contract), str(call.method)

        async def run_command() -> Any:
            provider = core.get_service(contract)
            return await getattr(provider, method)(**kwargs)

        return run_command

    # -- outcomes ---------------------------------------------------------
    async def _apply(
        self, step: SagaStep, direction: Direction, outcome: StepOutcome, result: Any
    ) -> str:
        row = self.row
        row.last_outcome = str(outcome.outcome)
        row.last_error = outcome.error_json()
        if outcome.outcome is Outcome.SUCCESS:
            return await self._on_success(step, direction, result)
        if outcome.outcome is Outcome.REFUSED:
            return await self._on_refused(direction)
        if outcome.outcome is Outcome.TRANSIENT:
            return await self._schedule_retry()
        if outcome.outcome is Outcome.CONFLICT:
            return await self.park(outcome.conflict_reason())
        return await self.park("misconfigured")

    async def _on_success(
        self, step: SagaStep, direction: Direction, result: Any
    ) -> str:
        row = self.row
        if direction == FORWARD:
            row.results[step.name] = to_jsonable_python(result)
            if step.name not in row.applied:
                row.applied.append(step.name)
            if row.resolving:
                return await self._resolved_applied(step)
            return await self.forward_to(row.step_index + 1)
        if step.name in row.applied:
            row.applied.remove(step.name)
        return await self.backward_from(row.step_index - 1)

    async def _resolved_applied(self, step: SagaStep) -> str:
        """Resolve-before-compensate found the step applied (§7)."""
        row = self.row
        row.resolving = False
        if step.kind == StepKind.COMPENSATABLE:
            return await self.backward_from(row.step_index)
        # The pivot turned out applied: past the go/no-go point there is only
        # forward recovery.
        row.status, row.direction = "running", "forward"
        row.compensation_deadline_at = None
        return await self.forward_to(row.step_index + 1)

    async def _on_refused(self, direction: Direction) -> str:
        row = self.row
        if direction == BACKWARD:
            return await self.park("compensation_refused")
        if row.resolving:  # the ambiguous forward call was in fact refused
            row.resolving = False
            return await self.backward_from(row.step_index - 1)
        if self._past_pivot():
            return await self.park("retriable_refused")
        return await self.begin_compensation(resolve_current=False)

    async def _schedule_retry(self) -> str:
        row = self.row
        base = float(self.settings.saga_retry_base_seconds)
        cap = float(self.settings.saga_retry_max_seconds)
        ceiling = min(cap, base * 2.0 ** max(0, row.attempt - 1))
        row.next_attempt_at = self.now + timedelta(
            seconds=ceiling * self.runtime.random()
        )
        await self.commit(nudge=False)
        return RETRY_SCHEDULED

    # -- transitions ------------------------------------------------------
    def _reset_step(self) -> None:
        row = self.row
        row.attempt = 0
        row.last_outcome = None
        row.last_error = None
        row.step_first_attempt_at = None
        row.step_deadline_at = None
        row.next_attempt_at = self.now

    async def forward_to(self, index: int) -> str:
        row = self.row
        self._reset_step()
        row.step_index = index
        if index >= len(self.definition.steps):
            row.status = "completed"
            row.step_index = len(self.definition.steps)
        await self.commit(nudge=row.status == "running")
        return ADVANCED

    async def backward_from(self, start: int) -> str:
        row = self.row
        self._reset_step()
        row.resolving = False
        row.step_index = next_applied_index(self.definition, row.applied, start)
        if row.step_index < 0:
            row.status = "compensated"
            row.step_index = -1
            row.compensation_deadline_at = None
        await self.commit(nudge=row.status == "compensating")
        return ADVANCED

    async def begin_compensation(self, *, resolve_current: bool) -> str:
        row = self.row
        row.status, row.direction = "compensating", "backward"
        row.compensation_deadline_at = self.now + compensation_window(
            self.reg, self.settings
        )
        row.step_deadline_at = None
        if resolve_current:
            # Resolve before compensating: re-issue the forward call with the
            # SAME key first — the command log replays, waits or runs it.
            row.resolving = True
            row.next_attempt_at = self.now
            await self.commit(nudge=True)
            return ADVANCED
        return await self.backward_from(row.step_index - 1)

    async def park(self, reason: str) -> str:
        row = self.row
        step = None
        if 0 <= row.step_index < len(self.definition.steps):
            step = self.definition.steps[row.step_index].name
        _mark_parked(row, reason)
        await self.commit(nudge=False)
        _audit_parked(row, reason, step, self.reg.database)
        return PARKED

    async def commit(self, *, nudge: bool) -> None:
        row = self.row
        row.version += 1
        row.updated_at = self.now
        await _store.update_instance(self.session, row)
        if nudge:
            await self.runtime.publish_nudge(
                self.reg, row.id, row.version, row.tenant_id
            )

    # -- bookkeeping --------------------------------------------------------
    async def _record_step(
        self, call: StepCall, outcome: StepOutcome, elapsed: float
    ) -> None:
        row = self.row
        status = {
            Outcome.SUCCESS: "succeeded",
            Outcome.REFUSED: "refused",
        }.get(outcome.outcome, "failed")
        await _store.upsert_step(
            self.session,
            {
                "instance_id": row.id,
                "step": call.step,
                "direction": call.direction,
                "saga_type": row.saga_type,
                "tenant_id": row.tenant_id,
                "status": status,
                "outcome": str(outcome.outcome),
                "attempts": row.attempt,
                "operation_key": call.key if call.contract is not None else None,
                "first_attempt_at": row.step_first_attempt_at,
                "last_attempt_at": self.now,
                "last_duration_ms": round(elapsed * 1000.0, 3),
                "last_error": outcome.error_json(),
            },
        )

    def _observe(self, call: StepCall, outcome: StepOutcome, elapsed: float) -> None:
        _metrics.saga_step_duration_seconds.labels(
            call.saga_type, call.direction
        ).observe(elapsed)
        _metrics.saga_step_attempts_total.labels(
            call.saga_type, str(outcome.outcome)
        ).inc()
        if outcome.ok and logger.isEnabledFor(logging.INFO):
            logger.info("saga step succeeded", extra=self._log_fields(call))

    def _log_fields(self, call: StepCall) -> dict[str, Any]:
        return {
            "saga_type": call.saga_type,
            "saga_id": call.saga_id,
            "step": call.step,
            "direction": call.direction,
            "attempt": call.attempt,
        }

    def _failure_fields(self, call: StepCall, outcome: StepOutcome) -> dict[str, Any]:
        return {
            **self._log_fields(call),
            "outcome": str(outcome.outcome),
            "status": outcome.status,
            "code": outcome.code,
        }


def _mark_parked(row: InstanceRow, reason: str) -> None:
    row.parked_from = row.status
    row.status = "parked"
    row.park_reason = reason


def _audit_parked(
    row: InstanceRow, reason: str, step: str | None, database: str | None
) -> None:
    """The park's audit row (required) + metric + log line (§7)."""
    record_audit(
        "core.saga.parked",
        actor_id=None,
        actor_type="system",
        resource_type="saga",
        resource_id=f"{row.saga_type}/{row.saga_id}",
        data={
            "reason": reason,
            "step": step,
            "direction": row.direction,
            "version": row.version,
        },
        required=True,
        database=database,
    )
    _metrics.saga_parked_total.labels(row.saga_type, reason).inc()
    logger.warning(
        "saga parked: %s",
        reason,
        extra={
            "saga_type": row.saga_type,
            "saga_id": row.saga_id,
            "step": step,
            "direction": row.direction,
            "attempt": row.attempt,
        },
    )


async def _park_unknown(session: AsyncSession, row: InstanceRow, now: datetime) -> str:
    """No ``(type, version)`` registered here: park, never crash the consumer."""
    _mark_parked(row, "unknown_definition")
    row.version += 1
    row.updated_at = now
    await _store.update_instance(session, row)
    _audit_parked(row, "unknown_definition", None, None)
    return PARKED


def _annotate(span: Any, row: InstanceRow, call: StepCall) -> None:
    """Link the step span to the saga (its start's trace id, §14)."""
    setter = getattr(span, "set_attribute", None)
    if setter is None:
        return
    setter("sandloom.saga.type", call.saga_type)
    setter("sandloom.saga.id", call.saga_id)
    setter("sandloom.saga.step", call.step)
    setter("sandloom.saga.direction", call.direction)
    setter("sandloom.saga.attempt", call.attempt)
    if row.trace_id:
        setter("sandloom.saga.start_trace_id", row.trace_id)


__all__ = [
    "ADVANCED",
    "NOOP_EARLY",
    "NOOP_LOCKED",
    "NOOP_STALE",
    "PARKED",
    "RETRY_SCHEDULED",
    "AfterCall",
    "Interceptor",
    "SagaHandle",
    "SagaInputConflict",
    "SagaNotInstalled",
    "SagaRuntime",
    "StepCall",
    "compensation_window",
    "next_applied_index",
    "parse_nudge",
    "record_may_have_expired",
    "ttl_margin_seconds",
    "utcnow",
]
