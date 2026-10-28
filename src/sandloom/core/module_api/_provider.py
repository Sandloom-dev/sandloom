"""Provider validation (:func:`contract_schema`, DTO placement) and the local
binding :class:`ContractProvider` returned by ``core.get_service``."""

from __future__ import annotations

import hashlib
import inspect
import threading
import typing
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Any

import orjson

import sandloom.core.module_api._context as _context
from sandloom.core import contract_cache_shadow, contract_wire
from sandloom.core.module_api._errors import normalize_contract_error
from sandloom.core.module_api._read_scope import _MEMO_MISS, _memo_key
from sandloom.core.module_api._schema import (
    ContractMethodRuntime,
    ContractSchema,
    _method_runtime,
)
from sandloom.core.module_api._spec import CONTRACT_SPEC_ATTR, contract_spec

# Hot receivers are bound by ASSIGNMENT, not by ``from ... import``: CPython
# (3.12+) compiles ``name.method()`` on an imported name without the
# method-call fast path (it assumes a module), so ``_var.get()`` would build a
# bound method on every call.
_contract_session_override = _context._contract_session_override
_contract_target = _context._contract_target
_read_scope_var = _context._read_scope_var


_SCHEMA_CACHE_MAX = 1024
_schema_cache: OrderedDict[int, tuple[Any, ContractSchema]] = OrderedDict()
_schema_cache_lock = threading.RLock()


def _is_classvar(ann: object) -> bool:
    origin: object = typing.get_origin(ann)
    classvar: object = typing.ClassVar
    return ann is classvar or origin is classvar


def _classvar_names(impl: Any) -> frozenset[str]:
    """Names annotated ``ClassVar[...]`` on the provider's classes: class
    constants, never exposed and never a contract method (spec §5.3)."""
    cls = impl if isinstance(impl, type) else type(impl)
    names: set[str] = set()
    for klass in cls.__mro__:
        for name, ann in inspect.get_annotations(klass).items():
            if isinstance(ann, str):
                head = ann.strip().strip("'\"").split("[", 1)[0].strip()
                if head in ("ClassVar", "typing.ClassVar", "t.ClassVar"):
                    names.add(name)
            elif _is_classvar(ann):
                names.add(name)
    return frozenset(names)


def contract_schema(impl: Any) -> ContractSchema:
    """The validated :class:`ContractSchema` of a contract provider (cached).

    Every public member must be a :func:`contract_method` with a
    wire-portable signature, or a ``ClassVar`` constant (spec §5.3: never
    exposed); private members (``_name``) are never exposed. Raises
    :class:`~sandloom.core.contract_wire.ContractTypeError` listing every problem —
    called at registration, so a non-portable contract never deploys.
    """
    if isinstance(impl, ContractProvider):
        impl = impl.contract_impl
    with _schema_cache_lock:
        cached = _schema_cache.get(id(impl))
        if cached is not None and cached[0] is impl:
            _schema_cache.move_to_end(id(impl))
            return cached[1]
    problems: list[str] = []
    methods: dict[str, ContractMethodRuntime] = {}
    constants = _classvar_names(impl)
    for name in dir(impl):
        if name.startswith("_") or name in constants:
            continue
        try:
            attr = getattr(impl, name)
        except Exception as exc:  # noqa: BLE001 - collected as a registration problem
            problems.append(f"{name}: cannot be read ({exc})")
            continue
        runtime = _method_runtime(name, attr, problems)
        if runtime is not None:
            methods[name] = runtime
    if problems:
        raise contract_wire.ContractTypeError(
            f"contract provider {impl!r} is not wire-portable: " + "; ".join(problems)
        )
    digest = hashlib.sha256(
        orjson.dumps(
            {name: m.describe() for name, m in methods.items()},
            option=orjson.OPT_SORT_KEYS,
        )
    ).hexdigest()[:32]
    schema = ContractSchema(methods=methods, hash=digest)
    with _schema_cache_lock:
        _schema_cache[id(impl)] = (impl, schema)
        while len(_schema_cache) > _SCHEMA_CACHE_MAX:
            _schema_cache.popitem(last=False)
    return schema


#: Packages whose DTOs every process may import (``CONTRACT_DTO_PACKAGES``
#: extends it; ``sandloom.shared`` is always shared).
DEFAULT_CONTRACT_DTO_PACKAGES: tuple[str, ...] = ("sandloom.shared",)


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def contract_dto_problems(  # noqa: C901
    name: str,
    impl: Any,
    *,
    provider_module: str | None,
    module_packages: Iterable[str] = (),
    shared_packages: Iterable[str] = (),
) -> list[str]:
    """DTOs of contract ``name`` no caller process could hold (SANDLOOM-C012).

    A provider authors its contract DTOs (pydantic models, Enums) in its own
    module package; ``sandloom generate`` copies each one a signature names
    into the shared DTO package, where every caller imports the copy (the
    stub names it, and a remote caller decodes into it). A DTO a module
    package holds is therefore allowed in its OWN provider's signatures; what
    no copy can come from is refused: a class of ANOTHER module's package
    (use its generated copy from the shared DTO package), or one defined in a
    function or ``__main__``. Classes of a shared package (``sandloom.shared``,
    ``CONTRACT_DTO_PACKAGES``) are referenced as they are. The closed set of
    DTO shapes is checked when ``@contract`` decorates the provider.

    Checked only for contracts a MODULE registers (``provider_module`` known);
    kernel-level test registrations are exempt.
    """
    if provider_module is None:
        return []
    try:
        schema = contract_schema(impl)
    except contract_wire.ContractTypeError:
        return []  # reported by the wire-type check
    shared = tuple(DEFAULT_CONTRACT_DTO_PACKAGES) + tuple(shared_packages)
    others = [m for m in module_packages if m != provider_module]
    problems: list[str] = []
    reported: set[int] = set()
    for method_name, method in sorted(schema.methods.items()):
        positions = [*method.annotations.items(), ("return", method.returns)]
        for position, annotation in positions:
            for tp in contract_wire.nominal_types(annotation):
                if id(tp) in reported:
                    continue
                module = getattr(tp, "__module__", "") or ""
                qualname = getattr(tp, "__qualname__", tp.__name__)
                if any(_within(module, pkg) for pkg in shared):
                    continue
                where: str | None = None
                if "<locals>" in qualname or module == "__main__":
                    where = "is defined in a function / __main__"
                elif not _within(module, provider_module):
                    owner = next((m for m in others if _within(module, m)), None)
                    if owner is not None:
                        where = f"lives in another module's package {owner!r}"
                if where is None:
                    continue
                reported.add(id(tp))
                kind = "Enum" if issubclass(tp, Enum) else "model"
                problems.append(
                    f"[SANDLOOM-C012] contract {name!r} {method_name}({position}): "
                    f"DTO {module}:{qualname} ({kind}) {where}, so `sandloom "
                    "generate` cannot give its callers a copy. Declare the DTO at "
                    "module level in the provider's own package (its schemas "
                    "module); another module's DTO is used through its generated "
                    "copy in the shared DTO package"
                )
    return problems


class ContractProvider:
    """A registered contract bound to its provider's core and database.

    Returned by ``core.get_service`` for a locally provided contract and used
    by the internal RPC server, so both paths run every call through
    :func:`run_contract_call`. Only the contract's exposed methods are visible
    (exactly what a remote proxy can call); each local call is bound with
    Python's rules, its arguments and result round-trip through the wire codec
    and are validated against the annotations, and provider errors surface as
    :class:`ContractCallError` — so a local call is observably identical to a
    remote one.
    """

    __slots__ = (
        "_sandloom_impl",
        "_sandloom_core",
        "_sandloom_database",
        "_sandloom_name",
        "_sandloom_schema",
        "_sandloom_methods",
    )

    def __init__(self, impl: Any, *, core: Any, database: str, name: str) -> None:
        self._sandloom_impl = impl
        self._sandloom_core = core
        self._sandloom_database = database
        self._sandloom_name = name
        self._sandloom_schema = contract_schema(impl)
        # Bound callables, built once per method (no closure per attribute
        # access on the hot path).
        self._sandloom_methods: dict[str, Any] = {}

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        cached = self._sandloom_methods.get(attr)
        if cached is not None:
            return cached
        runtime = self._sandloom_schema.methods.get(attr)
        if runtime is None:
            raise AttributeError(
                f"contract {self._sandloom_name!r} exposes no method {attr!r}"
            )
        method = self._sandloom_methods[attr] = self._local_method(runtime)
        return method

    def __dir__(self) -> list[str]:
        return sorted(self._sandloom_schema.methods)

    def __repr__(self) -> str:
        return f"<ContractProvider {self._sandloom_name} -> {self._sandloom_impl!r}>"

    @property
    def contract_impl(self) -> Any:
        """The registered provider object this binding wraps."""
        return self._sandloom_impl

    @property
    def contract_schema(self) -> ContractSchema:
        """The exposed methods (and hash) of this contract."""
        return self._sandloom_schema

    @property
    def contract_name(self) -> str:
        return self._sandloom_name

    def _local_method(self, runtime: ContractMethodRuntime) -> Any:
        call_local = self.call_local

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await call_local(runtime, args, kwargs)

        call.__name__ = runtime.name
        call.__qualname__ = f"{self._sandloom_name}.{runtime.name}"
        call.__signature__ = runtime.signature  # type: ignore[attr-defined]
        setattr(
            call,
            CONTRACT_SPEC_ATTR,
            contract_spec(getattr(self._sandloom_impl, runtime.name)),
        )
        return call

    async def call_local(
        self,
        runtime: ContractMethodRuntime,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """An in-process call: exactly what the provider/caller see over RPC.

        Arguments and result go through their :class:`~sandloom.core.contract_wire.
        WirePlan` (validated copies; immutable exact-type values pass through
        untouched). Inside a :class:`ContractReadScope` an identical read call
        is served from the scope's memo, and any command clears it.
        """
        label = f"{self._sandloom_name}.{runtime.name}"
        positional, keywords = runtime.bind(label, args, kwargs, local=True)
        scope = _read_scope_var.get()
        generation = scope.memo_generation if scope is not None else 0
        memo_key: Any = None
        if scope is not None:
            if runtime.kind != "read":
                scope.invalidate()
            elif scope.memo_enabled and _contract_session_override.get() is None:
                memo_key = _memo_key(
                    self._sandloom_name, runtime.name, positional, keywords
                )
                if memo_key is not None:
                    hit = scope.memo_get(memo_key)
                    if hit is not _MEMO_MISS:
                        return runtime._memo_result(label, hit)
        shadow = (
            contract_cache_shadow.for_contract(self._sandloom_name)
            if runtime.kind == "read"
            else None
        )
        started = shadow.start() if shadow is not None else None
        result = await self._run(runtime, positional, keywords)
        if scope is not None and runtime.kind != "read":
            scope.invalidate()
        local, wire = runtime._local_result_and_wire(label, result)
        if (
            memo_key is not None
            and scope is not None
            and scope.memo_generation == generation
        ):
            scope.memo_set(memo_key, runtime._memo_entry(label, local, wire))
        if shadow is not None and started is not None:
            # ADR-0003 shadow measurement: records what a caller-side cache
            # WOULD have done; never alters ``local`` (OFF by default).
            contract_cache_shadow.observe_read(
                shadow,
                self._sandloom_name,
                runtime.name,
                positional,
                keywords,
                local,
                started,
            )
        return local

    def encode_result(
        self,
        runtime: ContractMethodRuntime,
        result: Any,
        *,
        typed: bool = False,
    ) -> Any:
        """The tagged wire form of ``result`` (see :meth:`encode_result_as`)."""
        return self.encode_result_as(runtime, result, typed=typed)[1]

    def encode_result_as(
        self,
        runtime: ContractMethodRuntime,
        result: Any,
        *,
        typed: bool = False,
    ) -> tuple[int, Any]:
        """``(codec, wire)`` of ``result``; codec v2 when ``typed`` and it fits."""
        try:
            return runtime.result_plan.to_wire(result, typed=typed)
        except contract_wire.ContractWireError as exc:
            raise contract_wire.ContractWireError(
                f"{self._sandloom_name}.{runtime.name} returned a non-wire value: {exc}"
            ) from exc

    async def invoke(
        self,
        runtime: ContractMethodRuntime,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> Any:
        """Bind + validate UNTAGGED arguments and run the provider (both paths).

        Raises :class:`ContractCallError` for argument errors and for every
        provider ``HTTPException`` / ``http_status`` exception; anything else is
        a provider failure and propagates unchanged.
        """
        label = f"{self._sandloom_name}.{runtime.name}"
        positional, keywords = runtime.bind(label, args, kwargs)
        return await self._run(runtime, positional, keywords)

    async def _run(
        self,
        runtime: ContractMethodRuntime,
        positional: tuple[Any, ...],
        keywords: dict[str, Any],
    ) -> Any:
        """Run the provider on already bound + validated arguments."""
        method = getattr(self._sandloom_impl, runtime.name)
        try:
            token = _contract_target.set(
                (self._sandloom_core, self._sandloom_database, self._sandloom_name)
            )
            try:
                return await method(*positional, **keywords)
            finally:
                _contract_target.reset(token)
        except Exception as exc:
            normalized = normalize_contract_error(exc)
            if normalized is None or normalized is exc:
                raise
            raise normalized from exc


def bind_contract(impl: Any, *, core: Any, database: str, name: str) -> Any:
    """Wrap ``impl`` in a :class:`ContractProvider` (idempotent)."""
    if isinstance(impl, ContractProvider):
        return impl
    return ContractProvider(impl, core=core, database=database, name=name)
