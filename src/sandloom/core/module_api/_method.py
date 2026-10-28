"""The :func:`contract_method` decorator."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar, overload

import sandloom.core.module_api._context as _context
from sandloom.core.module_api._bootstrap import _require_core
from sandloom.core.module_api._call import _run_contract_call
from sandloom.core.module_api._spec import (
    CONTRACT_SPEC_ATTR,
    SESSION_PARAM_NAMES,
    ContractMethodSpec,
)
from sandloom.core.unit_of_work.risk import RISK_ATTR as _RISK_ATTR
from sandloom.core.unit_of_work.risk import RISKS as _RISKS

# Hot receivers are bound by ASSIGNMENT, not by ``from ... import``: CPython
# (3.12+) compiles ``name.method()`` on an imported name without the
# method-call fast path (it assumes a module), so ``_var.get()`` would build a
# bound method on every call.
_contract_target = _context._contract_target

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def _direct_command_target(
    wrapper: Callable[..., Any], qualname: str
) -> tuple[Any, str | None, str | None]:
    """``(core, database, contract)`` of a DIRECT command call.

    A command's durable dedupe record is keyed by its contract name and lives
    in its provider's database. A direct call (``Provider.spend(...)``,
    bypassing ``core.get_service``) of a REGISTERED provider uses that
    registration's name and database, so both paths share one record and the
    same operation key never runs twice. A provider registered under several
    names is ambiguous: the direct call is refused. An unregistered provider
    keeps its qualname namespace (tests, in-module use).
    """
    core = _require_core(None, qualname)
    registry: Any = getattr(core, "service_registry", None)
    if registry is None:
        return core, None, None
    found: set[tuple[str, str]] = set()
    for name in registry.list():
        impl = registry.try_get(name)
        impl = getattr(impl, "contract_impl", impl)
        attr = getattr(impl, wrapper.__name__, None)
        if getattr(attr, "__func__", attr) is wrapper:
            database = getattr(core, "contract_database", None)
            found.add((name, database(name) if database else "default"))
    if not found:
        return core, None, None
    if len(found) > 1:
        names = ", ".join(sorted(name for name, _ in found))
        raise RuntimeError(
            f"command {qualname!r} is registered as several contracts ({names}): "
            "a direct call cannot tell which dedupe namespace to use — call it "
            "through core.get_service(<name>)"
        )
    name, database = found.pop()
    return core, database, name


@overload
def contract_method(fn: _F) -> _F: ...


@overload
def contract_method(
    fn: None = None,
    *,
    command: bool = False,
    idempotency_key: str | None = None,
    idempotency_scope: tuple[str, ...] = (),
    database: str | None = None,
    planned: bool = False,
    risk: Literal["admin", "money", "security"] | None = None,
) -> Callable[[_F], _F]: ...


def contract_method(  # noqa: C901
    fn: _F | None = None,
    *,
    command: bool = False,
    idempotency_key: str | None = None,
    idempotency_scope: tuple[str, ...] = (),
    database: str | None = None,
    planned: bool = False,
    risk: Literal["admin", "money", "security"] | None = None,
) -> _F | Callable[[_F], _F]:
    """Mark an async method as a service-contract entry point.

    Every call — local (``core.get_service``) or remote (internal RPC server) —
    runs through :func:`run_contract_call`: a command's ``idempotency_key``
    argument is required, and the
    method body runs in the provider's own Unit of Work
    (:func:`contract_session`). Stack it UNDER ``@classmethod``.

    ``command=True`` commits the provider's UoW; such a call is NOT atomic with
    the caller's transaction (saga semantics — the caller compensates or uses
    events). ``database`` only applies to direct calls of an UNREGISTERED
    provider (tests): a direct command call of a registered provider runs on
    its registration's database under its contract name, sharing the
    registry path's dedupe record.

    ``idempotency_scope`` partitions a command's operation keys by named domain
    arguments, for example ``("tenant_type", "tenant_id")`` for personal and
    organization billing accounts. Changing this tuple changes the dedupe
    namespace; retain domain UNIQUE operation keys when rolling out a change.

    The signature must be wire-portable (checked when the provider is
    registered): every parameter and the return annotated with a contract wire
    type (:mod:`sandloom.core.contract_wire`), no ``*args`` / ``**kwargs``.

    ``planned=True`` (reads only) declares a contract whose body runs only
    declared planner operations through :func:`contract_reads`: no Unit of
    Work is opened; the reads lease from the contract pool in a read-only
    scope (``publish_event`` / ``record_audit`` raise), with the tenant bound
    exactly as for any contract call (the caller's locally, the verified RPC
    context remotely).

    ``risk`` (commands only) declares the risk of the command's effect at its
    source, once (ADR-0009 item 7): ``risk="money"`` for a command that moves
    money. It is recorded in the contract's describe document and snapshot;
    a local call in a request records the effect there (its own
    ``idempotency_key`` is its dedupe record), which triggers the automatic
    audit row of a sensitive write.
    """
    if risk is not None:
        if risk not in _RISKS:
            raise ValueError(f"risk must be one of {', '.join(_RISKS)}, not {risk!r}")
        if not command:
            raise ValueError("risk= applies to command contracts only")
    if planned and command:
        raise ValueError("planned=True applies to read contracts only")
    if idempotency_scope and not command:
        raise ValueError("idempotency_scope applies to command contracts only")
    if not isinstance(idempotency_scope, tuple) or any(
        not isinstance(name, str) or not name for name in idempotency_scope
    ):
        raise ValueError("idempotency_scope must be a tuple of parameter names")
    if len(set(idempotency_scope)) != len(idempotency_scope):
        raise ValueError("idempotency_scope contains duplicate parameter names")
    if command and not idempotency_key:
        raise ValueError(
            "a command contract must name its idempotency_key argument "
            "(e.g. @contract_method(command=True, idempotency_key='operation_key'))"
        )
    spec = ContractMethodSpec(
        kind="command" if command else "read",
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        database=database,
        planned=planned,
        risk=risk,
    )

    def decorate(func: _F) -> _F:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"@contract_method requires an async function: {func!r}")
        if idempotency_key is not None and (
            idempotency_key not in inspect.signature(func).parameters
        ):
            raise ValueError(
                f"{func.__qualname__}: idempotency_key {idempotency_key!r} is not "
                "a parameter of the method"
            )
        parameters = inspect.signature(func).parameters
        for scope_name in idempotency_scope:
            if scope_name not in parameters or scope_name in {
                "cls",
                "self",
                *SESSION_PARAM_NAMES,
            }:
                raise ValueError(
                    f"{func.__qualname__}: idempotency_scope {scope_name!r} is "
                    "not a domain parameter of the method"
                )

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            target = _contract_target.get()
            token = _contract_target.set(None)  # never leak into nested calls
            try:
                core: Any
                db_name: str | None
                contract: str | None
                if target is not None:
                    core, db_name, contract = target
                elif spec.kind == "command":
                    core, db_name, contract = _direct_command_target(
                        wrapper, func.__qualname__
                    )
                else:
                    core, db_name, contract = None, None, None
                return await _run_contract_call(
                    func,
                    args,
                    kwargs,
                    spec=spec,
                    core=core,
                    database=db_name or spec.database or "default",
                    contract=contract,
                )
            finally:
                _contract_target.reset(token)

        # Python 3.14's functools.wraps copies __annotate__, not
        # __annotations__ (PEP 649): a callable whose annotations were
        # assigned (generated fakes, decorators) would lose them, and the
        # wire-type check would see an unannotated contract.
        wrapper.__annotations__ = dict(getattr(func, "__annotations__", {}) or {})
        setattr(wrapper, CONTRACT_SPEC_ATTR, spec)
        if risk is not None:
            setattr(wrapper, _RISK_ATTR, risk)
        return wrapper  # type: ignore[return-value]

    if fn is not None:
        return decorate(fn)
    return decorate
