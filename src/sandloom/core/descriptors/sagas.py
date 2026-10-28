"""Saga definitions (ADR-0008 §1, §14) — pure data, no runtime.

A saga is a named, versioned, **linear** sequence of steps owned by one
module. Each step is a cross-service command (:func:`step`) or a function in
the owner's own Unit of Work (:func:`local_step`) and has a kind:

* ``compensatable`` — may be undone (every step before the pivot);
* ``pivot`` — the go/no-go point (at most one);
* ``retriable`` — after the pivot, must eventually succeed.

``Saga(...)`` checks the structure when it is built (duplicate names, kinds,
compensations, ``refuse_on``); ``core.register_saga`` binds it to its module
(``saga_type = "<module>.<name>"``) and the runtime (``sandloom.core.sagas``) checks
the called contracts at startup. Every error names the saga, the step and the
remedy (a ``fix:`` line), like capability errors.

Arguments are **named module-level functions** of a :class:`SagaContext`
(``c.input`` — the definition's own pydantic model — and ``c.result(step,
Model)``), recomputed on every attempt from the persisted input and results.
They must be pure: the durable command log's argument digest turns a
non-deterministic one into ``409`` and the instance parks
(``nondeterministic_args``).

This module is part of the stable public API (``sandloom.core.descriptors``,
re-exported by ``sandloom.core.module_api``).
"""

from __future__ import annotations

import functools
import re
import types
import typing
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Generic, Literal, TypeVar, overload

from pydantic import BaseModel, TypeAdapter

from sandloom.core.descriptors.module_names import module_name

__all__ = [
    "BACKWARD",
    "FORWARD",
    "CommandStep",
    "Direction",
    "LocalStep",
    "Saga",
    "SagaContext",
    "SagaDefinitionError",
    "SagaStep",
    "StepKind",
    "local_step",
    "operation_key",
    "saga_step_key",
    "step",
    "synthetic_instance",
]

InputT = TypeVar("InputT", bound=BaseModel)
T = TypeVar("T")

#: Direction of a step call: the forward effect or its compensation.
Direction = Literal["do", "undo"]
FORWARD: Direction = "do"
BACKWARD: Direction = "undo"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_CALL_RE = re.compile(
    r"^(?P<contract>(?:[a-z0-9_]+\.)+v\d+)\.(?P<method>[a-z_][a-z0-9_]*)$"
)
#: Statuses a step may never declare as a refusal: retrying them is safe and
#: the deadline bounds it (§7).
_NEVER_REFUSED = frozenset({408, 423, 425, 429})


class SagaDefinitionError(ValueError):
    """A saga definition is malformed or does not match its contracts.

    Raised when the definition is built (structure) and at startup (the called
    contracts); the message names the saga, the step and a ``fix:``.
    """

    fatal_at_startup = True


class StepKind(StrEnum):
    """The kind of a saga step (Garcia-Molina & Salem; Richardson)."""

    COMPENSATABLE = "compensatable"
    PIVOT = "pivot"
    RETRIABLE = "retriable"


def operation_key(*parts: object) -> str:
    """A derived operation key: ``":".join(parts)`` with checked parts.

    The one rule for keys derived from business ids (ADR-0008 §3): every part
    is non-empty and contains neither ``:`` nor whitespace, so two different
    tuples can never produce the same key. Use it for any command a flow
    issues more than once for the same business fact::

        operation_key("enroll", enrollment_id, "charge")  # "enroll:<id>:charge"
    """
    if not parts:
        raise ValueError("operation_key() needs at least one part")
    rendered: list[str] = []
    for part in parts:
        text = str(part)
        if not text or ":" in text or any(ch.isspace() for ch in text):
            raise ValueError(
                f"operation_key part {text!r} must be non-empty and contain "
                "no ':' or whitespace"
            )
        rendered.append(text)
    return ":".join(rendered)


def saga_step_key(saga_type: str, saga_id: str, step: str, direction: Direction) -> str:
    """The idempotency key the runtime injects into a step's command call.

    ``saga:<saga_type>:<saga_id>:<step>:do`` (forward) or ``…:undo``
    (compensation) — stable across attempts, so the provider's durable command
    log replays a step that already ran instead of running it twice.
    """
    return operation_key("saga", saga_type, saga_id, step, direction)


# --------------------------------------------------------------------------- #
# Synthetic values (registration-time argument check, §14)
# --------------------------------------------------------------------------- #
#: Representative value per wire scalar, most specific type first (``bool``
#: before ``int``, ``datetime`` before ``date``).
_SCALARS: tuple[tuple[type, object], ...] = (
    (bool, True),
    (int, 1),
    (float, 1.0),
    (Decimal, Decimal(1)),
    (str, "x"),
    (bytes, b"x"),
    (uuid.UUID, uuid.UUID(int=1)),
    (datetime, datetime(2000, 1, 1, tzinfo=UTC)),
    (date, date(2000, 1, 1)),
    (timedelta, timedelta(seconds=1)),
)


def _synthetic_scalar(tp: type) -> object:
    if issubclass(tp, Enum):
        return next(iter(tp))
    if issubclass(tp, BaseModel):
        return synthetic_instance(tp)
    for scalar, value in _SCALARS:
        if issubclass(tp, scalar):
            return value
    return None


def _synthetic(tp: Any) -> object:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Annotated:
        return _synthetic(args[0])
    if origin is Literal:
        return args[0] if args else None
    if origin in (typing.Union, types.UnionType):
        concrete = [a for a in args if a is not type(None)]
        return _synthetic(concrete[0]) if concrete else None
    if origin is not None:
        empties: dict[Any, object] = {
            list: [],
            tuple: (),
            set: set(),
            frozenset: frozenset(),
            dict: {},
        }
        return empties.get(
            origin,
            [] if isinstance(origin, type) and issubclass(origin, Sequence) else {},
        )
    if isinstance(tp, type):
        return _synthetic_scalar(tp)
    return "x"


def synthetic_instance(model: type[T]) -> T:
    """An instance of ``model`` built from its schema alone (no validation).

    Defaults are kept; required fields get a representative value of their
    type (``1``, ``"x"``, ``UUID(int=1)``, the first enum member, a nested
    synthetic model, an empty collection). Used to run a step's ``args``
    function once at startup (``check_args``); a function that cannot run on
    such input opts out per step with ``check_args=False``.
    """
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return typing.cast(T, _synthetic(model))
    values: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if info.is_required():
            values[name] = _synthetic(info.annotation)
    return typing.cast(T, model.model_construct(None, **values))


@functools.lru_cache(maxsize=512)
def _adapter(tp: Any) -> TypeAdapter[Any]:
    return TypeAdapter(tp)


# --------------------------------------------------------------------------- #
# The context handed to argument functions and local steps
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SagaContext(Generic[InputT]):
    """What a step's argument function (or local step) sees.

    ``input`` is the definition's input model; ``results`` maps each applied
    step's name to its persisted (JSON) result. ``key(step)`` is the derived
    operation key of a step (e.g. to pass the charge's key to the refund);
    ``result(step, Model)`` decodes a step's result to ``Model``.
    """

    saga_type: str
    saga_id: str
    input: InputT
    results: Mapping[str, Any] = field(default_factory=dict)
    #: Built from the input schema for the startup argument check: ``result``
    #: then answers with a synthetic value of the requested type.
    synthetic: bool = False

    def key(self, step: str, direction: Direction = FORWARD) -> str:
        """The operation key the runtime uses for ``step`` (``do`` / ``undo``)."""
        return saga_step_key(self.saga_type, self.saga_id, step, direction)

    @overload
    def result(self, step: str) -> Any: ...

    @overload
    def result(self, step: str, model: type[T]) -> T: ...

    def result(self, step: str, model: Any = None) -> Any:
        """The result ``step`` returned, decoded to ``model`` when given.

        Raises ``KeyError`` for a step that has not been applied (yet).
        """
        if self.synthetic:
            return synthetic_instance(model) if model is not None else None
        if step not in self.results:
            raise KeyError(f"saga {self.saga_type}: step {step!r} has no result yet")
        raw = self.results[step]
        if model is None:
            return raw
        return _adapter(model).validate_python(raw)


ArgsFn = Callable[[SagaContext[Any]], Mapping[str, Any]]
#: A local step: ``async (db, ctx) -> JSON-able result | None``, run in the
#: transition's own transaction (a savepoint: a failure rolls back its writes).
LocalFn = Callable[[Any, SagaContext[Any]], Awaitable[Any]]


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CommandStep:
    """A step that calls a command contract (``call="<contract>.<method>"``)."""

    name: str
    call: str
    args: ArgsFn
    compensate: str | None = None
    compensate_args: ArgsFn | None = None
    kind: StepKind | None = None
    #: Extra statuses this provider uses for a definitive refusal (e.g.
    #: ``{409}`` for a domain conflict). Never 408/423/425/429/5xx.
    refuse_on: frozenset[int] = frozenset()
    #: Run ``args`` once at startup on a synthetic context and check the names.
    check_args: bool = True
    #: Time from the step's first attempt after which it counts as timed out
    #: (default: only the saga deadline applies).
    deadline: timedelta | None = None

    @property
    def local(self) -> bool:
        return False

    @property
    def contract(self) -> str:
        return self.call.rsplit(".", 1)[0]

    @property
    def method(self) -> str:
        return self.call.rsplit(".", 1)[1]

    @property
    def compensate_contract(self) -> str | None:
        return self.compensate.rsplit(".", 1)[0] if self.compensate else None

    @property
    def compensate_method(self) -> str | None:
        return self.compensate.rsplit(".", 1)[1] if self.compensate else None

    @property
    def has_compensation(self) -> bool:
        return self.compensate is not None

    def undo_args(self) -> ArgsFn:
        """The compensation's argument function (default: the forward one)."""
        return self.compensate_args or self.args


@dataclass(frozen=True, slots=True)
class LocalStep:
    """A step run in the owner's own Unit of Work (transactional, §1)."""

    name: str
    apply: LocalFn
    compensate: LocalFn | None = None
    kind: StepKind | None = None
    deadline: timedelta | None = None

    @property
    def local(self) -> bool:
        return True

    @property
    def has_compensation(self) -> bool:
        return self.compensate is not None


SagaStep = CommandStep | LocalStep


def step(
    name: str,
    *,
    call: str,
    args: ArgsFn,
    compensate: str | None = None,
    compensate_args: ArgsFn | None = None,
    kind: StepKind | str | None = None,
    refuse_on: Iterable[int] = (),
    check_args: bool = True,
    deadline: timedelta | None = None,
) -> CommandStep:
    """A command step: ``call`` a command contract with ``args(ctx)``.

    The runtime injects the contract's ``idempotency_key`` argument with the
    derived key (``saga:<type>:<id>:<step>:do``), so ``args`` returns only the
    business arguments. ``compensate`` names the undo command (required for a
    compensatable step), called with ``compensate_args(ctx)`` (default
    ``args``) and the ``…:undo`` key; it must accept "nothing to undo".
    ``kind`` defaults to compensatable before the pivot, retriable after it.
    """
    return CommandStep(
        name=name,
        call=call,
        args=args,
        compensate=compensate,
        compensate_args=compensate_args,
        kind=StepKind(kind) if kind is not None else None,
        refuse_on=frozenset(int(s) for s in refuse_on),
        check_args=check_args,
        deadline=deadline,
    )


def local_step(
    name: str,
    *,
    apply: LocalFn,
    compensate: LocalFn | None = None,
    kind: StepKind | str | None = None,
    deadline: timedelta | None = None,
) -> LocalStep:
    """A local step: ``await apply(db, ctx)`` inside the transition's UoW.

    Its writes commit with the instance transition (transactional, no command
    log needed); raising rolls them back (a savepoint). Raise an exception
    with an ``http_status`` of 400/402/403/404/410/422 for a definitive
    refusal; anything else is retried. The returned value (JSON-able) is
    persisted as the step's result.
    """
    return LocalStep(
        name=name,
        apply=apply,
        compensate=compensate,
        kind=StepKind(kind) if kind is not None else None,
        deadline=deadline,
    )


# --------------------------------------------------------------------------- #
# The definition
# --------------------------------------------------------------------------- #
def _problem(saga: str, step_name: str | None, what: str, fix: str) -> str:
    where = f'{saga} step "{step_name}"' if step_name else saga
    return f"{where}: {what}\n  fix: {fix}"


def _check_command_step(saga: str, item: CommandStep) -> list[str]:
    problems: list[str] = []
    if not _CALL_RE.match(item.call):
        problems.append(
            _problem(
                saga,
                item.name,
                f"call={item.call!r} is not '<contract>.vN.<method>'",
                'name the provider\'s command, e.g. call="billing.credits.v1.spend"',
            )
        )
    if item.compensate is not None and not _CALL_RE.match(item.compensate):
        problems.append(
            _problem(
                saga,
                item.name,
                f"compensate={item.compensate!r} is not '<contract>.vN.<method>'",
                'name the undo command, e.g. compensate="billing.credits.v1.reverse"',
            )
        )
    if item.compensate_args is not None and item.compensate is None:
        problems.append(
            _problem(
                saga,
                item.name,
                "compensate_args without compensate",
                "name the compensation command or drop compensate_args",
            )
        )
    bad = sorted(s for s in item.refuse_on if s in _NEVER_REFUSED or not 400 <= s < 500)
    if bad:
        problems.append(
            _problem(
                saga,
                item.name,
                f"refuse_on={bad} would treat a transient status as a refusal",
                "refuse_on may only add 4xx business statuses (e.g. {409}); "
                "408/423/425/429/5xx are always retried until the deadline",
            )
        )
    return problems


def _resolve_kinds(
    saga: str, steps: Sequence[SagaStep]
) -> tuple[list[SagaStep], list[str]]:
    problems: list[str] = []
    pivots = [i for i, s in enumerate(steps) if s.kind == StepKind.PIVOT]
    if len(pivots) > 1:
        names = ", ".join(steps[i].name for i in pivots)
        problems.append(
            _problem(
                saga,
                None,
                f"more than one pivot ({names})",
                "a saga has at most one go/no-go step; make the others "
                "compensatable (before it) or retriable (after it)",
            )
        )
    pivot = pivots[0] if pivots else None
    resolved: list[SagaStep] = []
    for index, item in enumerate(steps):
        after = pivot is not None and index > pivot
        kind = item.kind or (StepKind.RETRIABLE if after else StepKind.COMPENSATABLE)
        if pivot is None or index < pivot:
            if kind != StepKind.COMPENSATABLE:
                problems.append(
                    _problem(
                        saga,
                        item.name,
                        f"a {kind} step before the pivot",
                        "every step before the pivot must be compensatable "
                        "(name its compensation), or move it after the pivot",
                    )
                )
        elif after and kind != StepKind.RETRIABLE:
            problems.append(
                _problem(
                    saga,
                    item.name,
                    f"a {kind} step after the pivot",
                    "steps after the pivot must be retriable (they must "
                    "eventually succeed); move it before the pivot",
                )
            )
        if kind == StepKind.COMPENSATABLE and not item.has_compensation:
            problems.append(
                _problem(
                    saga,
                    item.name,
                    "a compensatable step without a compensation",
                    "add compensate=... (a command that accepts 'nothing to "
                    "undo'), or make the step the pivot",
                )
            )
        if kind != StepKind.COMPENSATABLE and item.has_compensation:
            problems.append(
                _problem(
                    saga,
                    item.name,
                    f"a compensation on a {kind} step",
                    "only steps before the pivot are ever compensated; "
                    "drop compensate=",
                )
            )
        resolved.append(replace(item, kind=kind))
    return resolved, problems


def _check_steps(name: str, steps: Sequence[Any]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for item in steps:
        if not isinstance(item, CommandStep | LocalStep):
            problems.append(
                _problem(
                    name,
                    None,
                    f"{item!r} is not a step",
                    "build steps with step() / local_step()",
                )
            )
            continue
        if not _NAME_RE.match(item.name):
            problems.append(
                _problem(
                    name, item.name, "invalid step name", "use lowercase snake_case"
                )
            )
        if item.name in seen:
            problems.append(
                _problem(
                    name,
                    item.name,
                    "duplicate step name",
                    "give every step a unique name",
                )
            )
        seen.add(item.name)
        if isinstance(item, CommandStep):
            problems.extend(_check_command_step(name, item))
    return problems


def _check_structure(
    name: str, version: int, input_model: Any, deadline: timedelta, steps: Sequence[Any]
) -> list[str]:
    problems: list[str] = []
    if not _NAME_RE.match(name):
        problems.append(
            _problem(name, None, "invalid saga name", "use lowercase snake_case")
        )
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        problems.append(_problem(name, None, f"version={version!r}", "use an int >= 1"))
    if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
        problems.append(
            _problem(
                name,
                None,
                "input is not a pydantic model",
                "pass input=<BaseModel subclass>",
            )
        )
    if not isinstance(deadline, timedelta) or deadline <= timedelta(0):
        problems.append(
            _problem(
                name,
                None,
                "deadline must be a positive timedelta",
                "e.g. deadline=timedelta(minutes=30)",
            )
        )
    if not steps:
        problems.append(_problem(name, None, "no steps", "declare at least one step"))
    problems.extend(_check_steps(name, steps))
    return problems


class Saga(Generic[InputT]):
    """A saga definition: ``Saga[EnrollInput]("enroll", version=1, ...)``.

    Built once at import time in the module's ``sagas.py`` and registered in
    ``setup`` with ``core.register_saga(DEFINITION)``. A change to a
    registered definition is a new ``version`` registered alongside the old
    one for as long as old instances run (§11).
    """

    def __init__(
        self,
        name: str,
        *,
        version: int,
        input: type[InputT],
        deadline: timedelta,
        steps: Sequence[SagaStep],
        compensation_deadline: timedelta | None = None,
    ) -> None:
        problems = _check_structure(name, version, input, deadline, steps)
        resolved: list[SagaStep] = list(steps)
        if not problems:
            resolved, problems = _resolve_kinds(name, steps)
        if compensation_deadline is not None and compensation_deadline <= timedelta(0):
            problems.append(
                _problem(
                    name,
                    None,
                    "compensation_deadline must be positive",
                    "drop it or pass a positive timedelta",
                )
            )
        if problems:
            raise SagaDefinitionError("SagaDefinitionError: " + "\n".join(problems))
        self.name = name
        self.version = int(version)
        self.input_model: type[InputT] = input
        self.deadline = deadline
        self.compensation_deadline = compensation_deadline
        self.steps: tuple[SagaStep, ...] = tuple(resolved)
        self.pivot_index: int | None = next(
            (i for i, s in enumerate(self.steps) if s.kind == StepKind.PIVOT), None
        )
        self._saga_type: str | None = None

    def __repr__(self) -> str:
        return f"Saga({self._saga_type or self.name!r}, version={self.version})"

    # -- registration -----------------------------------------------------
    @property
    def saga_type(self) -> str:
        """``<module>.<name>`` — set when the definition is registered."""
        if self._saga_type is None:
            raise SagaDefinitionError(
                f"saga {self.name!r} is not registered: call "
                "core.register_saga(<definition>) in the module's setup"
            )
        return self._saga_type

    @property
    def registered(self) -> bool:
        return self._saga_type is not None

    def bind(self, module: str) -> str:
        """Bind the definition to its module (``register_saga`` calls this).

        ``module`` is the module's package (``sandloom_billing``) or short name; the
        saga type is ``<short>.<name>``. Re-binding to the same type is a
        no-op; binding one definition object to two modules is refused.
        """
        short = module_name(module)
        saga_type = f"{short}.{self.name}"
        if self._saga_type is not None and self._saga_type != saga_type:
            raise SagaDefinitionError(
                f"saga {self.name!r} is already registered as {self._saga_type!r}; "
                f"one definition belongs to one module (not {module!r})"
            )
        self._saga_type = saga_type
        return saga_type

    # -- queries ----------------------------------------------------------
    def step_named(self, name: str) -> SagaStep:
        for item in self.steps:
            if item.name == name:
                return item
        raise KeyError(f"saga {self.name}: no step {name!r}")

    def index_of(self, name: str) -> int:
        return self.steps.index(self.step_named(name))

    def describe(self) -> dict[str, Any]:
        """Diagnostics / snapshot shape: steps, kinds and called contracts."""
        return {
            "saga_type": self._saga_type,
            "name": self.name,
            "version": self.version,
            "deadline_seconds": self.deadline.total_seconds(),
            "compensation_deadline_seconds": (
                self.compensation_deadline.total_seconds()
                if self.compensation_deadline is not None
                else None
            ),
            "steps": [
                {
                    "name": s.name,
                    "kind": str(s.kind),
                    "local": s.local,
                    "call": None if isinstance(s, LocalStep) else s.call,
                    "compensate": (None if isinstance(s, LocalStep) else s.compensate),
                }
                for s in self.steps
            ],
        }
