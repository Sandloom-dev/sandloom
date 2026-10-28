"""Typed saga steps and discoverable saga definitions (L1, spec §3).

``step("charge", call=credits.spend, ...)`` names the command through the
caller's typed reference (``credits = Contract(BILLING_CREDITS_V1)``)
instead of the string ``"billing.credits.v1.spend"``: the L2 ``step`` is
called with that string, so the definition, its derived
``ContractRequirement`` and the startup check are today's. Every other
parameter of ``step`` and ``Saga`` is unchanged. A ``Saga`` built here
records its owning module, so ``define_module`` registers it from the
module's ``sagas`` submodule (``core.register_saga``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel

from sandloom.sdk._decl._owner import OWNER_ATTR, caller_module
from sandloom.sdk._decl.contracts import CALL_ATTR
from sandloom.sdk._prim.sagas import CommandStep, SagaStep, StepKind
from sandloom.sdk._prim.sagas import Saga as _Saga
from sandloom.sdk._prim.sagas import step as _step

InputT = TypeVar("InputT", bound=BaseModel)

#: A command: its ``"<contract>.<method>"`` name or a typed caller method
#: (``credits.spend`` of ``credits = Contract(BILLING_CREDITS_V1)``).
CommandRef = str | Callable[..., Awaitable[Any]]
ArgsFn = Callable[[Any], dict[str, Any]]


def command_name(ref: CommandRef, *, what: str) -> str:
    """The ``"<contract>.<method>"`` a command reference stands for."""
    if isinstance(ref, str):
        return ref
    name = getattr(ref, CALL_ATTR, None)
    if isinstance(name, str):
        return name
    raise TypeError(
        f"step({what}=...) expects '<contract>.<method>' or a contract caller "
        f"method such as credits.spend (credits = Contract(REF)), not {ref!r}"
    )


def step(
    name: str,
    *,
    call: CommandRef,
    args: ArgsFn,
    compensate: CommandRef | None = None,
    compensate_args: ArgsFn | None = None,
    kind: StepKind | str | None = None,
    refuse_on: Iterable[int] = (),
    check_args: bool = True,
    deadline: timedelta | None = None,
) -> CommandStep:
    """A command step: ``call`` a command contract with ``args(ctx)``::

        credits = Contract(BILLING_CREDITS_V1)
        step("charge", call=credits.spend, args=lambda ctx: {...},
             compensate=credits.grant)

    Identical to the L2 ``step`` with ``call="billing.credits.v1.spend"``.
    """
    return _step(
        name,
        call=command_name(call, what="call"),
        args=args,
        compensate=(
            command_name(compensate, what="compensate")
            if compensate is not None
            else None
        ),
        compensate_args=compensate_args,
        kind=kind,
        refuse_on=refuse_on,
        check_args=check_args,
        deadline=deadline,
    )


class Saga(_Saga[InputT]):
    """A saga definition (ADR-0008), discovered from the module's ``sagas``
    submodule; every parameter is today's."""

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
        super().__init__(
            name,
            version=version,
            input=input,
            deadline=deadline,
            steps=steps,
            compensation_deadline=compensation_deadline,
        )
        setattr(self, OWNER_ATTR, caller_module(1))


__all__ = ["CommandRef", "Saga", "command_name", "step"]
