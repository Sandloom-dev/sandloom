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

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel

from sandloom.sdk._decl._owner import OWNER_ATTR, caller_module
from sandloom.sdk._decl.contracts import CALL_ATTR
from sandloom.sdk._prim.sagas import (
    CommandStep,
    LocalStep,
    SagaContext,
    SagaStep,
    StepKind,
)
from sandloom.sdk._prim.sagas import Saga as _Saga
from sandloom.sdk._prim.sagas import local_step as _local_step
from sandloom.sdk._prim.sagas import step as _step

InputT = TypeVar("InputT", bound=BaseModel)

#: A command: its ``"<contract>.<method>"`` name or a typed caller method
#: (``credits.spend`` of ``credits = Contract(BILLING_CREDITS_V1)``).
CommandRef = str | Callable[..., Awaitable[Any]]
ArgsFn = Callable[[Any], dict[str, Any]]
#: What a local step runs: ``async def apply(ctx)``, writing through ``db()``.
LocalFn = Callable[[SagaContext[Any]], Awaitable[Any]]


def _takes_context_only(fn: LocalFn) -> bool:
    """Whether ``fn`` can be called with the context alone. Both signatures
    are read, the one a decorator shows (``__wrapped__`` followed) and the
    function's own, so a decorated ``apply(session, ctx)`` is refused and so
    is the adapter of a step already built (it needs the session first).
    (The kernel's ``sandloom.core.utils.takes_one_argument`` is the same
    rule; this layer imports no kernel module.)"""
    for follow_wrapped in (True, False):
        try:
            inspect.signature(fn, follow_wrapped=follow_wrapped).bind(None)
        except TypeError:
            return False
    return True


def _with_session(fn: LocalFn, *, what: str) -> Callable[..., Awaitable[Any]]:
    """``fn`` as the kernel's ``(session, ctx)`` function: the runtime hands a
    local step the transition's session first, which is ``db()`` already, so
    the author's function runs on the context alone. A function that needs the
    session as well is refused here, when the step is declared, not retried
    forever at run time."""
    if not _takes_context_only(fn):
        raise TypeError(
            f"local_step({what}=...) takes `async def f(ctx)` and writes through "
            "`db()`; a function that takes the session "
            f"(`async def f(session, ctx)`) is refused, not {fn!r}: drop the "
            "`session` parameter and call `db()` instead"
        )

    @functools.wraps(fn)
    async def run(_session: Any, ctx: SagaContext[Any]) -> Any:
        return await fn(ctx)

    return run


def local_step(
    name: str,
    *,
    apply: LocalFn,
    compensate: LocalFn | None = None,
    kind: StepKind | str | None = None,
    deadline: timedelta | None = None,
) -> LocalStep:
    """A local step, run inside the transition's unit of work::

        async def reserve_seat(ctx: SagaContext[EnrollInput]) -> dict[str, str]:
            db().add(Seat(course_id=ctx.input.course_id))
            return {"course_id": str(ctx.input.course_id)}

        local_step("reserve", apply=reserve_seat, compensate=release_seat)

    ``db()`` is the transition's session (a savepoint: raising rolls the step's
    writes back). A function that takes the session (``async def f(session,
    ctx)``) is refused with a ``TypeError`` when the step is declared. Raise
    ``NotFound(...)`` or ``Refused(status, ...)`` with status 400, 402, 403,
    404, 410 or 422 for a definitive refusal; ``Refused(401, ...)`` parks the
    saga as misconfigured; any other status is retried until the saga's
    deadline (``Conflict``, 409, included), as is any other exception. The
    returned value (JSON) is persisted as the step's result.
    """
    return _local_step(
        name,
        apply=_with_session(apply, what="apply"),
        compensate=(
            _with_session(compensate, what="compensate")
            if compensate is not None
            else None
        ),
        kind=kind,
        deadline=deadline,
    )


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


__all__ = ["CommandRef", "LocalFn", "Saga", "command_name", "local_step", "step"]
