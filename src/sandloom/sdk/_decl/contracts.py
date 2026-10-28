"""Typed contracts (L1, spec §2.3, §5.3, Appendix E).

Provider: ``@contract("x.y.v1")`` on a stateless class whose public
``async def`` methods become read contract methods; ``@command(...)`` marks
commands; ``@contract.read(planned=..., database=...)`` keeps today's read
options. Each method is built with ``contract_method`` under
``classmethod`` (the L2 form) and the class is registered with
``register_service`` by discovery.

Caller: ``Contract(X_Y_V1)`` (typed by the generated ``ContractRef``) or
``Contract(X_Y_V1, optional=True).client()``. ``define_module`` derives
``consumes`` from the module-level references.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, ParamSpec, Protocol, TypeVar, cast, overload

from sandloom.sdk._decl import _event_wire
from sandloom.sdk._decl._caller import converted, result_converter
from sandloom.sdk._decl._owner import OWNER_ATTR, caller_module
from sandloom.sdk._decl.errors import ContractDefinitionError, ContractDtoError
from sandloom.sdk._prim.contracts import (
    ContractRef,
    contract_method,
    contract_spec,
    service,
)

P = TypeVar("P")
C = TypeVar("C", bound=type)
PS = ParamSpec("PS")
R = TypeVar("R")

#: Attributes the decorators record their declaration in.
CONTRACT_ATTR = "__sandloom_contract__"
COMMAND_ATTR = "__sandloom_command__"
READ_ATTR = "__sandloom_read__"
#: The ``"<contract>.<method>"`` a bound caller method stands for (saga steps).
CALL_ATTR = "sandloom_call"


# --------------------------------------------------------------------------- #
# Callers
# --------------------------------------------------------------------------- #


class BoundContractCall:
    """``caller.method``: calls ``<contract>.<method>`` when awaited, and names
    it for a saga step (``step("s", call=credits.spend, ...)``). The result
    arrives as the stub's Protocol annotates it (the generated DTO copies),
    locally and over RPC (:mod:`sandloom.sdk._decl._caller`)."""

    __slots__ = ("sandloom_call", "contract", "method", "optional", "protocol")

    def __init__(
        self,
        contract: str,
        method: str,
        *,
        optional: bool = False,
        protocol: type | None = None,
    ) -> None:
        self.contract = contract
        self.method = method
        self.optional = optional
        self.protocol = protocol
        self.sandloom_call = f"{contract}.{method}"

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        provider = service(self.contract)
        call = getattr(provider, self.method)(*args, **kwargs)
        convert = result_converter(self.protocol, self.method)
        if convert is None:
            return call  # type: ignore[no-any-return]
        return converted(call, convert)

    def __repr__(self) -> str:
        return f"<contract call {self.sandloom_call}>"


class ContractCaller:
    """What ``Contract(REF)`` returns at run time: every attribute is a
    :class:`BoundContractCall` resolved when called (never at import)."""

    __slots__ = ("_sandloom_name", "_sandloom_protocol", OWNER_ATTR)

    def __init__(self, name: str, owner: str, protocol: type | None = None) -> None:
        self._sandloom_name = name
        self._sandloom_protocol = protocol
        setattr(self, OWNER_ATTR, owner)

    @property
    def contract_name(self) -> str:
        return self._sandloom_name

    @property
    def required(self) -> bool:
        return True

    def __getattr__(self, method: str) -> BoundContractCall:
        if method.startswith("_"):
            raise AttributeError(method)
        return BoundContractCall(
            self._sandloom_name, method, protocol=self._sandloom_protocol
        )

    def __repr__(self) -> str:
        return f"Contract({self._sandloom_name!r})"


class OptionalContract(Generic[P]):
    """``Contract(REF, optional=True)``: ``client()`` is ``None`` only when the
    provider is neither local nor bound in this deployment (decided at
    startup); a deployed provider that is down still raises (503).

    Use it only where the caller has a correct answer without the provider
    (a feature off, an enrichment skipped); a gate whose absence must deny is
    a required ``Contract(REF)`` (startup fails without it, SANDLOOM-B005).
    """

    def __init__(self, ref: ContractRef[P]) -> None:
        self.ref = ref
        setattr(self, OWNER_ATTR, caller_module(1))

    @property
    def contract_name(self) -> str:
        return self.ref.name

    @property
    def required(self) -> bool:
        return False

    def client(self) -> P | None:
        """The provider's proxy, or ``None`` when it is not deployed. Its
        results arrive as the stub annotates them, like ``Contract(REF)``'s."""
        provider: Any = service(self.ref.name, optional=True)
        protocol = getattr(self.ref, "protocol", None)
        if provider is None or protocol is None:
            return cast("P | None", provider)
        return cast("P", _TypedClient(provider, protocol))


class _TypedClient:
    """``Contract(REF, optional=True).client()`` of a stub with a Protocol:
    the provider's methods, their results converted like a
    :class:`BoundContractCall`'s."""

    __slots__ = ("_provider", "_protocol")

    def __init__(self, provider: Any, protocol: type) -> None:
        self._provider = provider
        self._protocol = protocol

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)
        target = getattr(self._provider, method)
        convert = result_converter(self._protocol, method)
        if convert is None:
            return target

        def call(*args: Any, **kwargs: Any) -> Awaitable[Any]:
            return converted(target(*args, **kwargs), convert)

        return call

    def __repr__(self) -> str:
        return repr(self._provider)


@overload
def Contract(ref: ContractRef[P]) -> P: ...
@overload
def Contract(
    ref: ContractRef[P], *, optional: Literal[True]
) -> OptionalContract[P]: ...
def Contract(ref: ContractRef[P], *, optional: bool = False) -> P | OptionalContract[P]:
    """The caller of contract ``ref`` (spec §5.3), declared at module level::

        quota = Contract(STATS_QUOTA_V1)          # required: startup fails without it
        if not await quota.may_create_note(): ...

    ``define_module`` adds it to the module's ``consumes`` (name and
    ``required`` exact; ``methods`` from the direct ``quota.<method>(`` calls in
    the defining module, else every method).
    """
    if not isinstance(ref, ContractRef):
        raise ContractDefinitionError(
            f"Contract() expects a generated ContractRef, not {ref!r}",
            fix="import the reference from the generated stub, e.g. "
            "from sandloom.shared.contracts.stats_quota_v1 import STATS_QUOTA_V1",
        )
    if optional:
        caller: OptionalContract[P] = OptionalContract.__new__(OptionalContract)
        caller.ref = ref
        setattr(caller, OWNER_ATTR, caller_module(1))
        return caller
    return ContractCaller(  # type: ignore[return-value]
        ref.name, caller_module(1), getattr(ref, "protocol", None)
    )


def contract_reference_name(value: Any) -> tuple[str, bool] | None:
    """``(name, required)`` of a ``Contract(...)`` object, else ``None``."""
    if isinstance(value, ContractCaller):
        return value.contract_name, True
    if isinstance(value, OptionalContract):
        return value.ref.name, False
    return None


def called_methods(source: str, names: set[str]) -> tuple[str, ...]:
    """The methods called directly on the module-level ``names`` (§5.3).

    Only ``<name>.<method>(`` on the name or a direct alias counts; any other
    use (passed on, stored, ``getattr``, ``.client()``) gives ``()``, which
    checks compatibility against every method: stricter, never weaker.
    """
    tree = ast.parse(source)
    aliases = set(names)
    alias_values: set[int] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and all(isinstance(t, ast.Name) for t in node.targets)
        ):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
            alias_values.add(id(node.value))
    methods: set[str] = set()
    direct_calls: set[int] = set(alias_values)
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in aliases
        ):
            if call.func.attr == "client" or call.func.attr.startswith("_"):
                return ()
            methods.add(call.func.attr)
            direct_calls.add(id(call.func.value))
    for use in ast.walk(tree):
        if not (isinstance(use, ast.Name) and use.id in aliases):
            continue
        if isinstance(use.ctx, ast.Store) or id(use) in direct_calls:
            continue
        return ()  # any other use: every method
    return tuple(sorted(methods))


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandSpec:
    idempotency_key: str
    idempotency_scope: tuple[str, ...]
    database: str | None
    risk: Literal["money"] | None


@dataclass(frozen=True)
class ReadSpec:
    planned: bool
    database: str | None


@dataclass(frozen=True)
class ContractSpec:
    name: str
    database: str | None


def command(
    *,
    idempotency_key: str,
    idempotency_scope: tuple[str, ...] = (),
    database: str | None = None,
    risk: Literal["money"] | None = None,
) -> Callable[[Callable[PS, R]], Callable[PS, R]]:
    """Mark a provider method as a command: deduplicated by its
    ``idempotency_key`` argument, committed in the provider's own unit, not
    atomic with the caller (a saga step)::

        @command(idempotency_key="operation_key")
        async def reserve(self, amount: int, operation_key: str) -> int: ...

    ``risk="money"`` declares a money source (ADR-0009).
    """
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ContractDefinitionError(
            "@command needs idempotency_key=<the parameter carrying the key>",
            fix='@command(idempotency_key="operation_key")',
        )
    if risk not in (None, "money"):
        raise ContractDefinitionError(
            f"@command(risk={risk!r}): the only command risk is 'money'",
            fix='risk="money" or no risk',
        )
    spec = CommandSpec(idempotency_key, tuple(idempotency_scope), database, risk)

    def decorate(fn: Callable[PS, R]) -> Callable[PS, R]:
        if idempotency_key not in inspect.signature(fn).parameters:
            raise ContractDefinitionError(
                f"@command on {getattr(fn, '__qualname__', fn)!r}: "
                f"idempotency_key {idempotency_key!r} is not a parameter",
                fix="add the parameter or name the right one",
            )
        setattr(fn, COMMAND_ATTR, spec)
        return fn

    return decorate


def _instance_attributes(member: Callable[..., Any]) -> list[str]:
    """``self.<x> = ...`` assignments in a method body (best effort)."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(member)))
    except (OSError, TypeError, SyntaxError):
        return []
    params = list(inspect.signature(member).parameters)
    if not params:
        return []
    first = params[0]
    found: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        found.extend(
            target.attr
            for target in targets
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == first
        )
    return found


def _build_method(
    where: str, attr: str, member: Callable[..., Any]
) -> classmethod[Any, Any, Any]:
    """The L2 form of one provider method: ``classmethod(contract_method(...))``."""
    if not inspect.iscoroutinefunction(member):
        raise ContractDefinitionError(
            f"{where}.{attr} is not async", fix=f"async def {attr}(self, ...)"
        )
    state = _instance_attributes(member)
    if state:
        raise ContractDefinitionError(
            f"{where}.{attr} assigns instance attributes {sorted(state)}: "
            "a provider is stateless",
            fix="keep state in the database or in private ClassVar constants",
        )
    cmd: CommandSpec | None = getattr(member, COMMAND_ATTR, None)
    read: ReadSpec | None = getattr(member, READ_ATTR, None)
    if cmd is not None and read is not None:
        raise ContractDefinitionError(
            f"{where}.{attr} is marked both @command and @contract.read",
            fix="a method is either a command or a read",
        )
    if cmd is not None:
        built = contract_method(
            command=True,
            idempotency_key=cmd.idempotency_key,
            idempotency_scope=cmd.idempotency_scope,
            database=cmd.database,
            risk=cmd.risk,
        )(member)
        setattr(built, COMMAND_ATTR, cmd)
    else:
        built = contract_method(
            planned=read.planned if read is not None else False,
            database=read.database if read is not None else None,
        )(member)
    return classmethod(built)


def _check_dtos(where: str, attr: str, member: Callable[..., Any]) -> None:
    """Every DTO a provider method's signature names is in the closed set of
    wire shapes and can be copied by ``sandloom generate`` (SANDLOOM-C012).
    An annotation that does not resolve yet is left to registration."""
    try:
        hints = typing.get_type_hints(member, include_extras=True)
    except (NameError, TypeError, AttributeError):  # refused at registration
        return
    for position, annotation in hints.items():
        for dto in _event_wire._dto_classes(annotation):
            try:
                _event_wire.check_contract_dto(dto, dto.__qualname__)
            except _event_wire.ShapeError as exc:
                raise ContractDtoError(
                    f"{where}.{attr}({position}): the contract DTO field "
                    f"{exc.where}: {exc.problem}",
                    fix=exc.fix,
                ) from None


def _build_provider(cls: type, spec: ContractSpec) -> None:
    where = f"@contract({spec.name!r}) {cls.__qualname__}"
    if "__init__" in cls.__dict__:
        raise ContractDefinitionError(
            f"{where} defines __init__: a provider is stateless",
            fix="remove __init__; keep constants private (_NAME: ClassVar[...])",
        )
    for attr, member in list(cls.__dict__.items()):
        if attr.startswith("_"):
            continue
        if isinstance(member, classmethod) and contract_spec(member.__func__):
            continue  # already the L2 form (@classmethod @contract_method)
        if inspect.isfunction(member):
            _check_dtos(where, attr, member)
            setattr(cls, attr, _build_method(where, attr, member))
            continue
        # The kernel's contract schema exposes only contract methods (it
        # refuses any other public member), so anything else stays private.
        raise ContractDefinitionError(
            f"{where}.{attr} is a public {type(member).__name__}: public members "
            "are async contract methods",
            fix=f"make it `async def {attr}(self, ...)` or private (_{attr})",
        )
    setattr(cls, CONTRACT_ATTR, spec)


def _read(
    *, planned: bool = False, database: str | None = None
) -> Callable[[Callable[PS, R]], Callable[PS, R]]:
    """``@contract.read(planned=..., database=...)``: keep today's read options
    on a provider method (``planned=True``: only declared planner reads, no
    unit; ``database=``)."""
    spec = ReadSpec(planned, database)

    def decorate(fn: Callable[PS, R]) -> Callable[PS, R]:
        setattr(fn, READ_ATTR, spec)
        return fn

    return decorate


class _ContractDecorator(Protocol):
    """The type of :func:`contract`: callable, with ``contract.read``."""

    def __call__(
        self, name: str, *, database: str | None = None
    ) -> Callable[[C], C]: ...

    @staticmethod
    def read(
        *, planned: bool = False, database: str | None = None
    ) -> Callable[[Callable[PS, R]], Callable[PS, R]]: ...


def _contract(name: str, *, database: str | None = None) -> Callable[[C], C]:
    """Declare a stateless provider class of contract ``name``::

        @contract("stats.quota.v1")
        class Quota:
            async def may_create_note(self) -> bool: ...

    Public ``async def`` methods are reads; ``@command(...)`` marks commands;
    ``@contract.read(planned=..., database=...)`` keeps today's read options.
    """
    if not isinstance(name, str) or name.count(".") < 2:
        raise ContractDefinitionError(
            f"@contract({name!r}): a contract name is <module>.<name>.vN",
            fix='@contract("stats.quota.v1")',
        )
    spec = ContractSpec(name, database)

    def decorate(cls: C) -> C:
        if not isinstance(cls, type):
            raise ContractDefinitionError(
                f"@contract({name!r}) decorates a class, not {cls!r}"
            )
        _build_provider(cls, spec)
        return cls

    return decorate


_contract.__name__ = _contract.__qualname__ = "contract"
_contract.read = _read  # type: ignore[attr-defined]
contract: _ContractDecorator = cast("_ContractDecorator", _contract)


def provider_spec(value: Any) -> ContractSpec | None:
    """The ``@contract`` declaration of a provider class, else ``None``."""
    if not isinstance(value, type):
        return None
    spec = value.__dict__.get(CONTRACT_ATTR)
    return spec if isinstance(spec, ContractSpec) else None


__all__ = [
    "CALL_ATTR",
    "BoundContractCall",
    "Contract",
    "ContractCaller",
    "ContractRef",
    "ContractSpec",
    "OptionalContract",
    "called_methods",
    "command",
    "contract",
    "contract_reference_name",
    "provider_spec",
]
