"""``saga_harness`` — drive the REAL saga protocol in a test, synchronously.

No sleeps, no consumers, no inline mode: every transition is the runtime's
own advance (row lock, outcome table, transition, outbox nudge) in a real,
committing Unit of Work on the test database, and every command step goes
through the provider's durable command log — so derived keys, replays and
409s behave exactly as in production::

    async def test_refund_when_access_cannot_be_granted(saga_harness):
        saga_harness.fail_step("grant_access", error="refused")
        run = await saga_harness.run(ENROLL, saga_id=..., input=...)
        assert run.status == "compensated"
        assert run.calls == ["charge:do", "grant_access:do", "charge:undo"]
        run.assert_keys_derived()

What it does instead of the infrastructure:

* **nudges** — the ones the runtime publishes are recorded and delivered in
  order; when none is pending (a backoff), the harness acts as the timer
  sweep: it advances the injected clock to the instance's next due time and
  nudges it (``auto_advance``);
* **faults** — ``fail_step(name, times=, error="transient"|"refused"|
  "timeout"|..., direction=)`` (``timeout`` lets the provider commit, then
  fails the call: the ambiguous case), ``crash_before`` / ``crash_after``
  (the transition rolls back — after the provider committed, for
  ``crash_after``), ``deliver_twice()``;
* **time** — ``clock.advance(timedelta)`` for deadlines and backoff (the
  jitter is fixed at ``0.5``);
* **providers** — the real ones when co-loaded, or
  ``fake_contract("billing.credits.v1", spend=..., reverse=...)``: plain
  async functions registered as command contracts (``@contract_method
  (command=True)``), so they still go through the command-log dedupe.

``assert_compensations_idempotent(definition, input=...)`` and
``assert_resolves_before_compensating(definition, input=...)`` are the two
one-liners a module's suite is expected to contain.
"""

from __future__ import annotations

import inspect
import typing
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

import sandloom.core.runtime as _core_runtime
import sandloom.core.sql_alchemy.saga_store as _store
from sandloom.core.descriptors import (
    BACKWARD,
    FORWARD,
    CommandStep,
    Direction,
    LocalStep,
    Saga,
    SagaContext,
    StepKind,
    synthetic_instance,
)
from sandloom.core.module_api import ContractCallError, contract_method
from sandloom.core.sagas import runtime_of
from sandloom.core.sagas.registry import (
    NUDGE_SCHEMA,
    RegisteredSaga,
    SagaRegistry,
    build_registry,
)
from sandloom.core.sagas.runtime import SagaHandle, SagaRuntime, StepCall, utcnow
from sandloom.core.sql_alchemy.models.saga import ACTIVE_STATUSES
from sandloom.core.sql_alchemy.saga_store import InstanceRow
from sandloom.core.tenant_context import bind_tenant, unscoped
from sandloom.core.testing.fixtures import _configured_driver, _required_env
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work import db_session

__all__ = [
    "FakeClock",
    "SagaHarness",
    "SagaRun",
    "SimulatedCrash",
    "open_saga_harness",
]

#: Harness-registered definitions without an explicit module get this one.
HARNESS_MODULE = "saga_tests"


class SimulatedCrash(BaseException):
    """The process "died": the transition's UoW rolls back (not an Exception,
    so the step classification never sees it)."""


class FakeClock:
    """The runtime's injectable clock (naive UTC)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now = self.now + delta
        return self.now

    def advance_to(self, when: datetime) -> datetime:
        if when > self.now:
            self.now = when
        return self.now


@dataclass(slots=True)
class _Fault:
    step: str
    direction: Direction
    error: str
    remaining: int
    status: int | None = None
    exception: BaseException | None = None


@dataclass(slots=True)
class SagaRun:
    """What one ``run()`` / ``drive()`` observed."""

    definition: Saga[Any]
    instance: InstanceRow
    #: ``"<step>:do"`` / ``"<step>:undo"`` per call attempt, in order.
    calls: list[str]
    #: The operation key each attempt carried (same order as ``calls``).
    keys: list[str]
    #: ``saga_steps`` rows of the instance.
    steps: list[dict[str, Any]]
    #: Advance results in order (``advanced`` / ``retry`` / ``noop:*`` / ...).
    transitions: list[str] = field(default_factory=list)
    crashes: int = 0

    @property
    def status(self) -> str:
        return self.instance.status

    @property
    def park_reason(self) -> str | None:
        return self.instance.park_reason

    @property
    def results(self) -> dict[str, Any]:
        return dict(self.instance.results)

    @property
    def saga_id(self) -> str:
        return self.instance.saga_id

    def step_row(self, step: str, direction: Direction = FORWARD) -> dict[str, Any]:
        for row in self.steps:
            if row["step"] == step and row["direction"] == direction:
                return row
        raise KeyError(f"no saga_steps row for {step}:{direction}")

    def assert_keys_derived(self) -> None:
        """Every command call carried ``saga:<type>:<id>:<step>:do|undo``."""
        commands = {s.name for s in self.definition.steps if isinstance(s, CommandStep)}
        for call, key in zip(self.calls, self.keys, strict=True):
            step, direction = call.split(":")
            if step not in commands:
                continue
            expected = (
                f"saga:{self.instance.saga_type}:{self.instance.saga_id}:"
                f"{step}:{direction}"
            )
            if key != expected:
                raise AssertionError(
                    f"{call} carried key {key!r}, expected {expected!r}"
                )


class _FakeProvider:
    """Namespace class the fake contract's methods are attached to."""


def _fake_contract_class(
    name: str, idempotency_key: str, methods: Mapping[str, Callable[..., Any]]
) -> type:
    attrs: dict[str, Any] = {"__module__": __name__, "__qualname__": f"Fake[{name}]"}
    for method_name, fn in methods.items():
        attrs[method_name] = _as_command(method_name, fn, idempotency_key)
    return type(f"Fake_{name.replace('.', '_')}", (_FakeProvider,), attrs)


def _as_command(method_name: str, fn: Callable[..., Any], key: str) -> Any:
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    params = [
        p.replace(annotation=hints.get(p.name, Any))
        for p in signature.parameters.values()
    ]
    if key not in signature.parameters:
        raise TypeError(
            f"fake {method_name}() must accept the idempotency key {key!r} "
            "(fake_contract(..., idempotency_key=...) names another one)"
        )
    is_async = inspect.iscoroutinefunction(fn)

    async def method(cls: Any, *args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        return await result if is_async else result

    method.__name__ = method_name
    method.__qualname__ = method_name
    method.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter("cls", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            *params,
        ],
        return_annotation=hints.get("return", Any),
    )
    method.__annotations__ = {**hints, "return": hints.get("return", Any)}
    return classmethod(contract_method(command=True, idempotency_key=key)(method))


class SagaHarness:
    """The ``saga_harness`` fixture's object (see the module docstring)."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.clock = FakeClock()
        app_runtime = runtime_of(core)
        self._app_registry = app_runtime.registry
        self.runtime = SagaRuntime(
            core,
            app_runtime.registry,
            clock=self.clock,
            random=lambda: 0.5,
            system_channel=app_runtime.system_channel,
        )
        self.runtime.interceptor = self._intercept
        self.runtime.after_call = self._after_call
        self._extra: dict[tuple[str, int], RegisteredSaga] = {}
        self._faults: list[_Fault] = []
        self._crash_before: list[_Fault] = []
        self._crash_after: list[_Fault] = []
        self._twice = False
        self._nudges: list[tuple[uuid.UUID, int]] = []
        self._calls: list[StepCall] = []
        self._fakes: list[str] = []
        #: Executions of each fake method body (NOT replays): ``"name.method"``.
        self.fake_calls: dict[str, list[dict[str, Any]]] = {}
        self.auto_advance = True
        self._wrap_publish()

    # -- setup ------------------------------------------------------------
    def _wrap_publish(self) -> None:
        publish = self.runtime.publish_nudge

        async def recording(
            reg: RegisteredSaga,
            instance_id: uuid.UUID,
            version: int,
            tenant_id: uuid.UUID | None,
        ) -> None:
            await publish(reg, instance_id, version, tenant_id)
            self._nudges.append((instance_id, version))

        self.runtime.publish_nudge = recording  # type: ignore[method-assign]

    def register(
        self, definition: Saga[Any], *, module: str = HARNESS_MODULE
    ) -> RegisteredSaga:
        """Register a definition for this test (checked like at startup)."""
        if definition.registered:
            found = self._registry().get(definition.saga_type, definition.version)
            if found is not None and found.definition is definition:
                return found
        definition.bind(module)
        shim = _CoreShim(self.core, [(definition, module)])
        registered = next(iter(build_registry(shim)))
        self._extra[(registered.saga_type, registered.version)] = registered
        channel = registered.channel
        if self.core.get_event_schema(channel) is None:
            self.core.register_event_schema(channel, channel, NUDGE_SCHEMA)
        self.runtime.registry = self._registry()
        return registered

    def _registry(self) -> SagaRegistry:
        return SagaRegistry([*self._app_registry, *self._extra.values()])

    def _ensure(self, definition: Saga[Any]) -> RegisteredSaga:
        if definition.registered:
            found = self._registry().get(definition.saga_type, definition.version)
            if found is not None and found.definition is definition:
                return found
        return self.register(definition)

    def fake_contract(
        self,
        name: str,
        *,
        idempotency_key: str = "operation_key",
        **methods: Callable[..., Any],
    ) -> type:
        """Register ``name`` as a command contract made of plain functions.

        Each function takes the business arguments plus the idempotency key
        (``operation_key`` unless renamed) and is wrapped in
        ``@contract_method(command=True)`` — its calls go through the durable
        command log like a real provider's. ``fake_calls["name.method"]``
        records the executions (a replay does not execute the body).
        """
        recorded = {
            method: self._recording(f"{name}.{method}", fn)
            for method, fn in methods.items()
        }
        provider = _fake_contract_class(name, idempotency_key, recorded)
        self.core.register_service(name, provider)
        self._fakes.append(name)
        return provider

    def _recording(self, label: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        calls = self.fake_calls.setdefault(label, [])
        is_async = inspect.iscoroutinefunction(fn)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls.append(dict(kwargs))
            result = fn(*args, **kwargs)
            return await result if is_async else result

        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        wrapper.__annotations__ = typing.get_type_hints(fn)
        wrapper.__name__ = getattr(fn, "__name__", "fake")
        return wrapper

    def close(self) -> None:
        for name in self._fakes:
            self.core.service_registry.remove(name)
            self.core._bound_contracts.pop(name, None)
        self._fakes.clear()

    # -- faults -----------------------------------------------------------
    def fail_step(
        self,
        name: str,
        *,
        times: int = 1,
        error: str = "transient",
        direction: Direction = FORWARD,
        status: int | None = None,
        exception: BaseException | None = None,
    ) -> None:
        """Fail the next ``times`` calls of ``name`` (``direction``).

        ``error``: ``transient`` (503 before the call), ``refused`` (422
        before the call), ``timeout`` (the provider runs and commits, then
        the caller sees a timeout — ambiguous), ``misconfigured`` (401),
        ``conflict`` (409 ``ContractIdempotencyConflict``) — or pass the
        ``exception`` to raise (before the call).
        """
        self._faults.append(_Fault(name, direction, error, times, status, exception))

    def crash_before(
        self, name: str, *, direction: Direction = FORWARD, times: int = 1
    ) -> None:
        """The process dies before the step call: nothing ran, the UoW rolls back."""
        self._crash_before.append(_Fault(name, direction, "crash", times))

    def crash_after(
        self, name: str, *, direction: Direction = FORWARD, times: int = 1
    ) -> None:
        """The provider committed, then the process died before our commit."""
        self._crash_after.append(_Fault(name, direction, "crash", times))

    def deliver_twice(self, enabled: bool = True) -> None:
        """Deliver every nudge twice (the duplicate must be a no-op)."""
        self._twice = enabled

    @staticmethod
    def _take(faults: list[_Fault], call: StepCall) -> _Fault | None:
        for fault in faults:
            if (
                fault.step == call.step
                and fault.direction == call.direction
                and fault.remaining
            ):
                fault.remaining -= 1
                return fault
        return None

    async def _intercept(
        self, call: StepCall, proceed: Callable[[], Awaitable[Any]]
    ) -> Any:
        self._calls.append(call)
        if self._take(self._crash_before, call) is not None:
            raise SimulatedCrash(call.label)
        fault = self._take(self._faults, call)
        if fault is None:
            return await proceed()
        if fault.exception is not None:
            raise fault.exception
        if fault.error == "timeout" and call.contract is not None:
            await proceed()  # the provider committed; the caller never hears
            raise TimeoutError(
                f"harness: {call.label} timed out after the provider ran"
            )
        raise _fault_error(fault)

    def _after_call(self, call: StepCall) -> None:
        if self._take(self._crash_after, call) is not None:
            raise SimulatedCrash(call.label)

    # -- driving ----------------------------------------------------------
    async def start(
        self,
        definition: Saga[Any],
        *,
        saga_id: object,
        input: BaseModel | Mapping[str, Any],
        tenant_id: uuid.UUID | None = None,
    ) -> SagaHandle:
        """``start_saga`` in a committing UoW (the caller's business change)."""
        reg = self._ensure(definition)
        with bind_tenant(tenant_id) if tenant_id is not None else nullcontext():
            async with db_session(reg.database):
                return await self.runtime.start(
                    definition, saga_id=saga_id, input=input
                )

    async def run(
        self,
        definition: Saga[Any],
        *,
        saga_id: object | None = None,
        input: BaseModel | Mapping[str, Any],
        tenant_id: uuid.UUID | None = None,
        max_transitions: int = 500,
        stop_when: Callable[[InstanceRow], bool] | None = None,
    ) -> SagaRun:
        """Start ``definition`` and drive it until it settles (terminal/parked)."""
        sid = saga_id if saga_id is not None else uuid.uuid4()
        handle = await self.start(
            definition, saga_id=sid, input=input, tenant_id=tenant_id
        )
        return await self.drive(
            definition,
            handle.saga_id,
            max_transitions=max_transitions,
            stop_when=stop_when,
        )

    async def drive(
        self,
        definition: Saga[Any],
        saga_id: object,
        *,
        max_transitions: int = 500,
        stop_when: Callable[[InstanceRow], bool] | None = None,
    ) -> SagaRun:
        """Drive an existing instance until it settles (or ``stop_when``)."""
        reg = self._ensure(definition)
        sid = str(saga_id)
        first_call = len(self._calls)
        transitions: list[str] = []
        crashes = 0
        for _ in range(max_transitions):
            row = await self.instance(reg, sid)
            if row.status not in ACTIVE_STATUSES or (stop_when and stop_when(row)):
                break
            version = self._pending_version(row)
            if version is None:
                if not self.auto_advance:
                    break
                self.clock.advance_to(_next_due(row))
                version = row.version
            for _delivery in range(2 if self._twice else 1):
                try:
                    transitions.append(await self.deliver(reg, row, version))
                except SimulatedCrash:
                    crashes += 1
                    transitions.append("crash")
                    break
        else:
            raise AssertionError(
                f"{reg.saga_type} {sid!r} did not settle in {max_transitions} "
                "transitions"
            )
        return await self._snapshot(reg, sid, first_call, transitions, crashes)

    def _pending_version(self, row: InstanceRow) -> int | None:
        """The newest published nudge of ``row`` that is still current."""
        due_now = row.next_attempt_at <= self.clock.now
        for instance_id, version in reversed(self._nudges):
            if instance_id == row.id:
                return version if version == row.version and due_now else None
        return None

    async def deliver(self, reg: RegisteredSaga, row: InstanceRow, version: int) -> str:
        """Deliver one nudge through the real advance path (its own UoW)."""
        envelope = {
            "id": str(uuid.uuid4()),
            "payload": {
                "instance_id": str(row.id),
                "expected_version": version,
                "tenant_id": str(row.tenant_id) if row.tenant_id else None,
            },
        }
        async with db_session(reg.database) as session:
            return await self.runtime.advance_nudge(session, envelope)

    async def instance(
        self, reg: RegisteredSaga | Saga[Any], saga_id: object
    ) -> InstanceRow:
        saga_type = reg.saga_type
        database = (
            reg.database
            if isinstance(reg, RegisteredSaga)
            else self._ensure(reg).database
        )
        async with _unscoped_session(database) as session:
            row = await _store.find_instance(
                session, saga_type, str(saga_id), tenant=_store.ANY_TENANT
            )
        if row is None:
            raise AssertionError(f"no {saga_type} instance {saga_id!r}")
        return row

    async def _snapshot(
        self,
        reg: RegisteredSaga,
        saga_id: str,
        first_call: int,
        transitions: list[str],
        crashes: int,
    ) -> SagaRun:
        row = await self.instance(reg, saga_id)
        async with _unscoped_session(reg.database) as session:
            steps = await _store.steps_of(session, row.id)
        calls = [c for c in self._calls[first_call:] if c.saga_id == saga_id]
        return SagaRun(
            definition=reg.definition,
            instance=row,
            calls=[c.label for c in calls],
            keys=[c.key for c in calls],
            steps=steps,
            transitions=transitions,
            crashes=crashes,
        )

    # -- the two one-liners -------------------------------------------------
    async def assert_compensations_idempotent(
        self,
        definition: Saga[Any],
        *,
        input: BaseModel | Mapping[str, Any] | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> None:
        """Every compensation runs once per applied step, survives a repeat
        and accepts "nothing to undo".

        For each compensatable step ``k``: the next step refuses, so steps
        ``0..k`` apply and are compensated (``compensated``, one ``undo`` per
        applied step, in reverse). Then each compensation is called again with
        its key (a replay must succeed) and on a fresh saga id whose forward
        call never ran (nothing to undo — it must not fail).
        """
        payload = _input_of(definition, input)
        steps = definition.steps
        for index, item in enumerate(steps):
            if item.kind != StepKind.COMPENSATABLE or index + 1 >= len(steps):
                continue
            self.fail_step(steps[index + 1].name, error="refused")
            run = await self.run(definition, input=payload, tenant_id=tenant_id)
            expected = [f"{s.name}:undo" for s in reversed(steps[: index + 1])]
            undo = [c for c in run.calls if c.endswith(":undo")]
            if run.status != "compensated" or undo != expected:
                raise AssertionError(
                    f"{definition.saga_type}: refusing {steps[index + 1].name!r} gave "
                    f"{run.status} with compensations {undo}, expected {expected}"
                )
            await self._repeat_compensations(run, steps[: index + 1], payload)

    async def _repeat_compensations(
        self, run: SagaRun, applied: tuple[Any, ...] | list[Any], payload: Any
    ) -> None:
        reg = self._ensure(run.definition)
        for item in applied:
            for saga_id in (run.saga_id, f"nothing-{uuid.uuid4().hex[:12]}"):
                ctx = SagaContext(
                    saga_type=reg.saga_type,
                    saga_id=saga_id,
                    input=reg.parse_input(_dump(payload)),
                    results=run.results,
                )
                try:
                    await self._call_compensation(
                        reg, item, ctx, run.instance.tenant_id
                    )
                except Exception as exc:
                    raise AssertionError(
                        f"{reg.saga_type}: compensation of {item.name!r} failed when "
                        f"repeated / with nothing to undo (saga id {saga_id!r}): "
                        f"{exc!r}"
                    ) from exc

    async def _call_compensation(
        self,
        reg: RegisteredSaga,
        item: Any,
        ctx: SagaContext[Any],
        tenant: uuid.UUID | None,
    ) -> None:
        with bind_tenant(tenant) if tenant is not None else nullcontext():
            async with db_session(reg.database) as session:
                if isinstance(item, LocalStep):
                    if item.compensate is not None:
                        async with session.begin_nested():
                            await item.compensate(session, ctx)
                    return
                args = dict(item.undo_args()(ctx))
                args[reg.key_params[(item.name, BACKWARD)]] = ctx.key(
                    item.name, BACKWARD
                )
                contract, method = str(item.compensate).rsplit(".", 1)
                await getattr(self.core.get_service(contract), method)(**args)

    async def assert_resolves_before_compensating(
        self,
        definition: Saga[Any],
        *,
        input: BaseModel | Mapping[str, Any] | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> None:
        """An ambiguous step is re-issued (same key) before anything is undone.

        For every command step up to the pivot: its first call commits at the
        provider but times out for the caller; the saga deadline then passes.
        The runtime must re-issue the forward call (``<step>:do`` again, same
        key: the command log replays) and then compensate it as APPLIED —
        never compensate blindly, never skip it.
        """
        payload = _input_of(definition, input)
        pivot = definition.pivot_index
        last = pivot if pivot is not None else len(definition.steps) - 1
        for item in definition.steps[: last + 1]:
            if not isinstance(item, CommandStep):
                continue
            self.fail_step(item.name, error="timeout")
            name = item.name

            def attempted(row: InstanceRow, name: str = name) -> bool:
                return _attempted(row, definition, name)

            run = await self.run(
                definition, input=payload, tenant_id=tenant_id, stop_when=attempted
            )
            self.clock.advance(definition.deadline + timedelta(seconds=1))
            settled = await self.drive(definition, run.saga_id)
            calls = run.calls + settled.calls
            first = calls.index(f"{item.name}:do")
            if f"{item.name}:do" not in calls[first + 1 :]:
                raise AssertionError(
                    f"{definition.saga_type}: {item.name!r} was not re-issued before "
                    f"compensating (calls {calls})"
                )
            resolved_at = calls.index(f"{item.name}:do", first + 1)
            undo = f"{item.name}:undo"
            if item.kind == StepKind.COMPENSATABLE and (
                undo not in calls or calls.index(undo) < resolved_at
            ):
                raise AssertionError(
                    f"{definition.saga_type}: {item.name!r} must be compensated after "
                    f"it was resolved as applied (calls {calls})"
                )


def _attempted(row: InstanceRow, definition: Saga[Any], name: str) -> bool:
    index = definition.index_of(name)
    return row.step_index == index and row.attempt > 0


def _fault_error(fault: _Fault) -> Exception:
    if fault.error == "refused":
        return ContractCallError("harness: refused", status_code=fault.status or 422)
    if fault.error == "misconfigured":
        return HTTPException(status_code=401, detail="harness: misconfigured")
    if fault.error == "conflict":
        return ContractCallError(
            "harness: conflict", status_code=409, code="contract_idempotency_conflict"
        )
    return ContractCallError("harness: transient", status_code=fault.status or 503)


def _next_due(row: InstanceRow) -> datetime:
    candidates = [row.next_attempt_at]
    if row.status == "running":
        candidates.append(row.deadline_at)
        if row.step_deadline_at is not None:
            candidates.append(row.step_deadline_at)
    elif row.compensation_deadline_at is not None:
        candidates.append(row.compensation_deadline_at)
    return min(candidates)


def _dump(payload: Any) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return dict(payload)


def _input_of(definition: Saga[Any], given: Any) -> Any:
    if given is not None:
        return given
    return synthetic_instance(definition.input_model)


@asynccontextmanager
async def _unscoped_session(database: str) -> AsyncIterator[Any]:
    with unscoped():
        async with db_session(database) as session:
            yield session


class _CoreShim:
    """``build_registry`` input for harness-registered definitions."""

    def __init__(self, core: Any, sagas: list[tuple[Saga[Any], str]]) -> None:
        self._core = core
        self.sagas = sagas
        self.module_descriptors: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._core, name)


@asynccontextmanager
async def open_saga_harness(core: Any) -> AsyncIterator[SagaHarness]:
    """The fixture body: point ``default`` at the test database, yield a harness."""
    async with AsyncExitStack() as stack:
        stack.enter_context(_core_runtime.use_active_core(core))
        await stack.enter_async_context(
            override_instance(
                core,
                "database",
                driver=_configured_driver(core, "database", "sqlalchemy"),
                url=_required_env("DATABASE_URL"),
            )
        )
        harness = SagaHarness(core)
        try:
            yield harness
        finally:
            harness.close()
