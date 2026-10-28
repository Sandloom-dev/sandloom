"""The registered sagas of a process, checked against their contracts (§1, §14).

``core.register_saga`` only stores a definition on the facade (``module_api``
never imports this package). After every module loaded, the composition root
builds a :class:`SagaRegistry` from them: it resolves each definition's
module, database and nudge channel, and checks every command step
deterministically against the provider's **describe document** — the local
provider's, or for a remote provider the snapshot shipped with its package
(``contracts.snapshot.json`` / ``CONTRACT_SNAPSHOT_PATHS``; the readiness
compat check compares that snapshot with the live provider):

* the called method exists and is a **command** with an ``idempotency_key``
  (the runtime injects it);
* the parameter **names** each ``args`` function returns match the method's
  signature — the function is called once on a synthetic
  :class:`~sandloom.core.descriptors.SagaContext` built from the input schema
  (``check_args=False`` opts a step out).

Every problem names the saga, the step and a ``fix:``; all of them are raised
together as one :class:`~sandloom.core.descriptors.SagaDefinitionError` (startup
aborts).
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

import sandloom.core.contract_compat as _contract_compat
from sandloom.core.descriptors import (
    BACKWARD,
    FORWARD,
    CommandStep,
    Direction,
    Saga,
    SagaContext,
    SagaDefinitionError,
    synthetic_instance,
)
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.module_api import contract_schema

logger = logging.getLogger("sandloom.sagas")

#: Payload schema of every ``<module>.saga_advance.v1`` nudge.
NUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "instance_id": {"type": "string"},
        "expected_version": {"type": "integer"},
        "tenant_id": {"type": ["string", "null"]},
    },
    "required": ["instance_id", "expected_version"],
}

#: The kernel-installed advance consumer of each saga-owning module.
ADVANCE_CONSUMER = "sagas@advance"


def nudge_channel(short: str) -> str:
    """The owning module's private advance channel."""
    return f"{short}.saga_advance.v1"


@dataclass(frozen=True, slots=True)
class RegisteredSaga:
    """A definition bound to its module, database and nudge channel."""

    definition: Saga[Any]
    module: str
    short: str
    database: str
    #: ``(step, direction) -> idempotency-key parameter`` of each command call.
    key_params: Mapping[tuple[str, Direction], str] = field(default_factory=dict)

    @property
    def saga_type(self) -> str:
        return self.definition.saga_type

    @property
    def version(self) -> int:
        return self.definition.version

    @property
    def channel(self) -> str:
        return nudge_channel(self.short)

    def parse_input(self, raw: Mapping[str, Any]) -> BaseModel:
        model: type[BaseModel] = self.definition.input_model
        return model.model_validate(dict(raw))


class SagaRegistry:
    """``(saga_type, version) -> RegisteredSaga`` of one process."""

    def __init__(self, sagas: Iterable[RegisteredSaga] = ()) -> None:
        self._by_key: dict[tuple[str, int], RegisteredSaga] = {}
        for item in sagas:
            self._by_key[(item.saga_type, item.version)] = item

    def __iter__(self) -> Iterator[RegisteredSaga]:
        return iter(self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, saga_type: str, version: int) -> RegisteredSaga | None:
        return self._by_key.get((saga_type, int(version)))

    def of(self, definition: Saga[Any]) -> RegisteredSaga:
        found = self.get(definition.saga_type, definition.version)
        if found is None or found.definition is not definition:
            raise SagaDefinitionError(
                f"saga {definition!r} is not registered in this process: "
                "register it with core.register_saga in its module's setup"
            )
        return found

    def modules(self) -> dict[str, tuple[str, str]]:
        """``module -> (short, database)`` of every saga-owning module."""
        return {r.module: (r.short, r.database) for r in self}

    def databases(self) -> list[str]:
        return sorted({r.database for r in self})

    def describe(self) -> list[dict[str, Any]]:
        """``/diagnostics/core`` rows."""
        return [
            {**r.definition.describe(), "module": r.module, "database": r.database}
            for r in sorted(self, key=lambda r: (r.saga_type, r.version))
        ]


# --------------------------------------------------------------------------- #
# Contract checks
# --------------------------------------------------------------------------- #
def _describe(core: Any, contract: str) -> tuple[dict[str, Any] | None, str]:
    """The describe document of ``contract`` and where it came from."""
    impl = core.service_registry.try_get(contract)
    if impl is not None:
        return contract_schema(impl).describe(), "local provider"
    found = _contract_compat.expected_contracts(core, [contract]).get(contract)
    if found is not None:
        return found.describe, f"snapshot {found.source}"
    return None, "none"


def _where(saga: Saga[Any], step: CommandStep, direction: Direction) -> str:
    call = step.call if direction == FORWARD else step.compensate
    role = "" if direction == FORWARD else " (compensation)"
    return f'{saga.saga_type} step "{step.name}"{role}: {call}'


def _args_problems(
    saga: Saga[Any],
    step: CommandStep,
    direction: Direction,
    method: Mapping[str, Any],
    key_param: str,
) -> list[str]:
    fn: Callable[..., Any] = step.args if direction == FORWARD else step.undo_args()
    fn_name = getattr(fn, "__name__", repr(fn))
    ctx: SagaContext[Any] = SagaContext(
        saga_type=saga.saga_type,
        saga_id="00000000-0000-0000-0000-000000000001",
        input=synthetic_instance(saga.input_model),
        synthetic=True,
    )
    where = _where(saga, step, direction)
    try:
        produced: Any = fn(ctx)
    except Exception as exc:
        logger.debug("args check of %s raised", where, exc_info=True)
        return [
            f"{where}\n  {fn_name}() raised {type(exc).__name__}: {exc} on a "
            "synthetic input\n  fix: make it a pure function of c.input / "
            "c.result(...), or pass check_args=False on the step (then only "
            "tests cover it)"
        ]
    if not isinstance(produced, Mapping):
        return [
            f"{where}\n  {fn_name}() returned {type(produced).__name__}, not a "
            "mapping\n  fix: return a dict of the method's parameter names"
        ]
    params = {p["name"]: p for p in method.get("params", [])}
    problems: list[str] = []
    if key_param in produced:
        problems.append(
            f"{where}\n  {fn_name}() passes the idempotency key {key_param!r}\n"
            "  fix: drop it — the runtime injects the derived key "
            "(saga:<type>:<id>:<step>:do|undo)"
        )
    for name in sorted(set(produced) - set(params)):
        hint = difflib.get_close_matches(name, list(params), n=1)
        problems.append(
            f'{where} has no parameter "{name}"'
            + (f' (did you mean "{hint[0]}"?)' if hint else "")
            + f"\n  fix: return the provider's parameter names from {fn_name}()"
        )
    missing = sorted(
        n
        for n, p in params.items()
        if p.get("required") and n != key_param and n not in produced
    )
    problems.extend(
        f'{where}: required parameter "{name}" is not supplied\n'
        f"  fix: return it from {fn_name}()"
        for name in missing
    )
    return problems


def _call_problems(
    core: Any, saga: Saga[Any], step: CommandStep, direction: Direction
) -> tuple[str | None, list[str]]:
    """``(idempotency-key parameter, problems)`` of one command call."""
    contract = step.contract if direction == FORWARD else step.compensate_contract
    method_name = step.method if direction == FORWARD else step.compensate_method
    where = _where(saga, step, direction)
    if contract is None or method_name is None:
        return None, [f"{where}\n  no compensation command declared"]
    doc, _source = _describe(core, contract)
    if doc is None:
        return None, [
            f"{where}\n  no describe document for {contract!r}: it is neither "
            "provided in this process nor covered by a contract snapshot\n"
            "  fix: load the provider module, or bind it in SERVICE_BINDINGS "
            "and ship its contracts.snapshot.json (or CONTRACT_SNAPSHOT_PATHS)"
        ]
    methods: Mapping[str, Any] = doc.get("methods", {})
    method = methods.get(method_name)
    if method is None:
        hint = difflib.get_close_matches(method_name, list(methods), n=1)
        return None, [
            f"{where}\n  {contract} has no method {method_name!r}"
            + (f' (did you mean "{hint[0]}"?)' if hint else "")
            + f"\n  fix: name one of {sorted(methods)}"
        ]
    key_param = method.get("idempotency_key")
    if method.get("kind") != "command" or not key_param:
        return None, [
            f"{where}\n  {contract}.{method_name} is a {method.get('kind')} "
            "contract, not a command with an idempotency_key\n  fix: call a "
            "@contract_method(command=True, idempotency_key=...) method — a "
            "saga step must be exactly-once at the provider"
        ]
    problems = (
        _args_problems(saga, step, direction, method, str(key_param))
        if step.check_args
        else []
    )
    return str(key_param), problems


def _module_database(core: Any, module: str) -> str:
    for desc in getattr(core, "module_descriptors", None) or ():
        if getattr(desc, "name", None) == module:
            instances = sorted(desc.database_instances())
            return str(instances[0]) if instances else "default"
    return "default"


def build_registry(core: Any) -> SagaRegistry:
    """Bind and check every saga registered on ``core`` (startup, no I/O)."""
    problems: list[str] = []
    registered: list[RegisteredSaga] = []
    seen: set[tuple[str, int]] = set()
    for definition, module in getattr(core, "sagas", None) or ():
        key = (definition.saga_type, definition.version)
        if key in seen:
            problems.append(
                f"{definition.saga_type} v{definition.version}: registered twice\n"
                "  fix: register each (name, version) once; a change is a new version"
            )
            continue
        seen.add(key)
        key_params: dict[tuple[str, Direction], str] = {}
        for item in definition.steps:
            if not isinstance(item, CommandStep):
                continue
            directions: list[Direction] = [FORWARD]
            if item.has_compensation:
                directions.append(BACKWARD)
            for direction in directions:
                param, found = _call_problems(core, definition, item, direction)
                problems.extend(found)
                if param is not None:
                    key_params[(item.name, direction)] = param
        registered.append(
            RegisteredSaga(
                definition=definition,
                module=module,
                short=_module_name(module),
                database=_module_database(core, module),
                key_params=key_params,
            )
        )
    if problems:
        raise SagaDefinitionError("SagaDefinitionError: " + "\n".join(problems))
    return SagaRegistry(registered)


__all__ = [
    "ADVANCE_CONSUMER",
    "NUDGE_SCHEMA",
    "RegisteredSaga",
    "SagaRegistry",
    "build_registry",
    "nudge_channel",
]
