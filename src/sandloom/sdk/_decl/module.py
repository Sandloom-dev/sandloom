"""``define_module`` with discovery (L1, spec §3, §5.7).

``register = define_module(__name__)`` builds the module's L2
``register(core: ModuleAPI) -> ModuleDescriptor``. ``register`` imports the
conventional submodules (``models``, ``api``, ``events``, ``consumers``,
``contracts``, ``sagas``, ``projections``, ``tasks``; a package is walked
recursively, sorted by name), collects the declarations the module owns and
makes exactly the calls a hand-written ``register`` would:

=====================================  =========================================
declaration                            L2 call
=====================================  =========================================
``api.router`` / ``api.__all__``       ``core.include_router(router)`` (today's
routers, ``module_router()``           rule for plain routers, unchanged)
producer ``class E(Event, ...)``       ``core.register_event_schema(C, name, schema)``
``@on(E)``                             ``core.register_event_consumer(C, cb, ...)``
``@contract("x.y.v1")``                ``core.register_service("x.y.v1", P)``
``Saga(...)``                          ``core.register_saga(saga)``
``Projection(...)``                    ``register_query_projection`` +
                                       ``register_projection_consumer`` (+ rebuilder)
module-level ``Contract(REF)``         ``ContractRequirement`` in ``consumes``
=====================================  =========================================

Discovery validates everything before the first call, so a refused module
registers nothing (the loader's draft is never committed, SANDLOOM-B001).
Every parameter of today's ``define_module`` is kept.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, cast

from fastapi import APIRouter

from sandloom.sdk._decl._owner import (
    CONVENTIONAL,
    OWNER_ATTR,
    caller_module,
    short_name,
    within,
)
from sandloom.sdk._decl.contracts import (
    ContractSpec,
    called_methods,
    contract_reference_name,
    provider_spec,
)
from sandloom.sdk._decl.errors import (
    ConsumerDefinitionError,
    ContractDefinitionError,
    EventDefinitionError,
    ModuleDiscoveryError,
)
from sandloom.sdk._decl.events import (
    CONSUMERS_ATTR,
    ConsumerSpec,
    Event,
    consumer_callback,
    is_concrete,
    resolve_role,
    wire_schema,
)
from sandloom.sdk._decl.projections import Projection, register_view_source
from sandloom.sdk._decl.sagas import Saga
from sandloom.sdk._decl.web import ModuleRouter
from sandloom.sdk._prim.module import (
    AUTO,
    CapabilityRequirement,
    ContractRequirement,
    ModuleAPI,
    ModuleDescriptor,
)

#: Where each kind of declaration belongs (``SANDLOOM-B006`` names the place).
_HOMES = {
    "event": ("events", "consumers", "projections", "sagas", "api", "contracts"),
    "consumer": ("consumers",),
    "contract": ("contracts",),
    "router": ("api",),
    "saga": ("sagas",),
    "projection": ("projections",),
}


def _optional_module(dotted: str) -> ModuleType | None:
    try:
        spec = importlib.util.find_spec(dotted)
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        return None
    return importlib.import_module(dotted)


def _walk(module: ModuleType) -> Iterator[ModuleType]:
    yield module
    path = getattr(module, "__path__", None)
    if path is None:
        return
    infos = sorted(
        pkgutil.walk_packages(path, prefix=module.__name__ + "."), key=lambda i: i.name
    )
    for info in infos:
        if ".tests" in info.name[len(module.__name__) :] or info.name.endswith(
            ".tests"
        ):
            continue
        if ".migrations" in info.name[len(module.__name__) :]:
            continue
        yield importlib.import_module(info.name)


def import_conventional(package: str) -> list[ModuleType]:
    """Import ``package``'s conventional submodules in order (spec §5.7)."""
    imported: list[ModuleType] = []
    for name in CONVENTIONAL:
        module = _optional_module(f"{package}.{name}")
        if module is not None:
            imported.extend(_walk(module))
    return imported


def _submodule_kind(module: str, package: str) -> str | None:
    """The conventional submodule ``module`` lies in (``None``: outside)."""
    rest = module[len(package) + 1 :] if module != package else ""
    head = rest.split(".", 1)[0]
    return head if head in CONVENTIONAL else None


def _owned_modules(package: str) -> list[ModuleType]:
    """Every imported module of ``package`` (tests and migrations excluded)."""
    out = []
    for name in sorted(sys.modules):
        if not within(name, package):
            continue
        rest = name[len(package) :]
        if ".tests" in rest or ".migrations" in rest:
            continue
        module = sys.modules.get(name)
        if module is not None:
            out.append(module)
    return out


@dataclass
class Discovered:
    """What discovery found in one module package (in registration order)."""

    package: str
    routers: list[APIRouter] = field(default_factory=list)
    producers: list[type[Event]] = field(default_factory=list)
    consumers: list[ConsumerSpec] = field(default_factory=list)
    providers: list[tuple[ContractSpec, type]] = field(default_factory=list)
    sagas: list[Saga[Any]] = field(default_factory=list)
    projections: list[Projection] = field(default_factory=list)
    requirements: list[ContractRequirement] = field(default_factory=list)


def _misplaced(kind: str, what: str, module: str, package: str) -> ModuleDiscoveryError:
    homes = " or ".join(f"{package}.{h}" for h in _HOMES[kind])
    return ModuleDiscoveryError(
        f"{what} is declared in {module}, outside the module's conventional "
        "submodules, so discovery would not register it",
        fix=f"move it to {homes}",
    )


def _place(kind: str, what: str, module: str, package: str) -> None:
    if _submodule_kind(module, package) not in _HOMES[kind]:
        raise _misplaced(kind, what, module, package)


def _module_references(package: str, module: ModuleType) -> dict[str, set[str]]:
    """``{contract: {module-level names}}`` of the ``Contract(...)`` objects
    ``module`` defines (re-exports are counted where they are defined)."""
    names: dict[str, set[str]] = {}
    for attr, value in vars(module).items():
        ref = contract_reference_name(value)
        if ref is not None and getattr(value, OWNER_ATTR, None) == module.__name__:
            names.setdefault(ref[0], set()).add(attr)
    return names


def _requirements(package: str, modules: list[ModuleType]) -> list[ContractRequirement]:
    """The derived ``consumes`` (spec §5.3): name and ``required`` exact,
    ``methods`` from direct calls (any other use: every method)."""
    required: dict[str, bool] = {}
    methods: dict[str, tuple[str, ...]] = {}
    for module in modules:
        names = _module_references(package, module)
        if not names:
            continue
        try:
            source: str | None = inspect.getsource(module)
        except (OSError, TypeError):
            source = None
        for contract_name, attrs in names.items():
            for attr in attrs:
                ref = contract_reference_name(vars(module)[attr])
                required[contract_name] = required.get(contract_name, False) or bool(
                    ref and ref[1]
                )
            called = called_methods(source, attrs) if source is not None else ()
            previous = methods.get(contract_name)
            if previous is None:
                methods[contract_name] = called
            elif previous and called:
                methods[contract_name] = tuple(sorted(set(previous) | set(called)))
            else:
                methods[contract_name] = ()
    return [
        ContractRequirement(name, required=required[name], methods=methods[name])
        for name in sorted(required)
    ]


class _Collector:
    """Classifies the values of a module package's namespaces (spec §5.7)."""

    def __init__(self, package: str) -> None:
        self.package = package
        self.found = Discovered(package)
        self.seen: set[int] = set()
        self.consumer_names: dict[str, str] = {}
        self.producer_channels: dict[str, type[Event]] = {}

    def visit(self, module: ModuleType, value: Any) -> None:
        if id(value) in self.seen:
            return
        if isinstance(value, type):
            if value.__module__ != module.__name__:
                return
            if issubclass(value, Event):
                self._event(value)
            elif provider_spec(value) is not None:
                self._provider(value)
            return
        if callable(value) and getattr(value, CONSUMERS_ATTR, None):
            if getattr(value, "__module__", None) == module.__name__:
                self._consumers(value)
            return
        owner = getattr(value, OWNER_ATTR, None)
        if isinstance(owner, str) and within(owner, self.package):
            self._instance(value, owner)

    def _event(self, event: type[Event]) -> None:
        if not is_concrete(event):
            return
        self.seen.add(id(event))
        _place("event", f"event {event.__qualname__}", event.__module__, self.package)
        if resolve_role(event, self.package) != "producer":
            return
        other = self.producer_channels.get(event.channel)
        if other is not None:
            raise EventDefinitionError(
                f"{self.package} defines two producers of {event.channel!r}: "
                f"{other.__qualname__} and {event.__qualname__}",
                fix='one producer class per channel; others are views (role="view")',
            )
        self.producer_channels[event.channel] = event
        self.found.producers.append(event)

    def _consumers(self, fn: Any) -> None:
        self.seen.add(id(fn))
        for spec in getattr(fn, CONSUMERS_ATTR):
            _place("consumer", f"@on consumer {spec.name}", fn.__module__, self.package)
            if spec.name in self.consumer_names:
                raise ConsumerDefinitionError(
                    f"{self.package}: two consumers are named {spec.name!r} "
                    f"({self.consumer_names[spec.name]} and {fn.__qualname__})",
                    fix='give one an explicit name: @on(E, name="...")',
                )
            self.consumer_names[spec.name] = fn.__qualname__
            self.found.consumers.append(spec)

    def _provider(self, cls: type) -> None:
        spec = provider_spec(cls)
        if spec is None:
            return
        self.seen.add(id(cls))
        what = f"@contract({spec.name!r}) {cls.__qualname__}"
        _place("contract", what, cls.__module__, self.package)
        _check_bases(cls, self.package, spec)
        self.found.providers.append((spec, cls))

    def _instance(self, value: Any, owner: str) -> None:
        if isinstance(value, ModuleRouter):
            self.seen.add(id(value))
            _place("router", "a module_router()", owner, self.package)
            self.found.routers.append(value)
        elif isinstance(value, Saga):
            self.seen.add(id(value))
            _place("saga", f"saga {value.name!r}", owner, self.package)
            self.found.sagas.append(value)
        elif isinstance(value, Projection):
            self.seen.add(id(value))
            _place("projection", f"projection {value.name!r}", owner, self.package)
            self.found.projections.append(value)


def discover(package: str) -> Discovered:
    """Import ``package``'s conventional submodules and collect what it owns.

    Raises (SANDLOOM-B006 / B015 / B016 / B018) before anything is registered.
    """
    import_conventional(package)
    modules = _owned_modules(package)
    collector = _Collector(package)
    for module in modules:
        for value in list(vars(module).values()):
            collector.visit(module, value)
    collector.found.requirements = _requirements(package, modules)
    return collector.found


def _check_bases(cls: type, package: str, spec: ContractSpec) -> None:
    for base in cls.__mro__[1:]:
        if base is object:
            continue
        if not within(base.__module__, package):
            raise ContractDefinitionError(
                f"@contract({spec.name!r}) {cls.__qualname__} inherits "
                f"{base.__module__}.{base.__qualname__}, outside {package}",
                fix="inherit only from classes inside the module package",
            )


def plain_routers(package: str) -> list[APIRouter]:
    """Today's rule (``_define.py``): ``<pkg>.api.router``, else every
    ``APIRouter`` among ``api.__all__`` (or its public names)."""
    api = _optional_module(f"{package}.api")
    if api is None:
        return []
    explicit = getattr(api, "router", None)
    if isinstance(explicit, APIRouter):
        return [explicit]
    names = getattr(api, "__all__", None)
    if names is None:
        names = [n for n in vars(api) if not n.startswith("_")]
    routers: list[APIRouter] = []
    for attr in names:
        value = getattr(api, attr, None)
        if isinstance(value, APIRouter) and all(value is not r for r in routers):
            routers.append(value)
    return routers


def _event_schemas(package: str) -> Mapping[str, dict[str, Any] | None]:
    events = _optional_module(f"{package}.events")
    schemas = getattr(events, "EVENT_SCHEMAS", None) if events is not None else None
    if schemas is None:
        return {}
    if not isinstance(schemas, Mapping):
        raise TypeError(
            f"{package}.events.EVENT_SCHEMAS must map channel -> JSON schema, "
            f"got {type(schemas).__name__}"
        )
    return cast("Mapping[str, dict[str, Any] | None]", schemas)


def event_schema(event: type[Event]) -> dict[str, Any]:
    """The registered (and exported) schema of a producer class: its wire
    schema (serialization mode, open to added fields; :func:`wire_schema`)."""
    return wire_schema(event)


def _policy_requirements(routers: Sequence[APIRouter]) -> list[ContractRequirement]:
    """Contracts the routes' authorization policies may call (optional).

    ``require(...)`` / ``authorize_resource(...)`` tag their dependency with
    the policy; each atom names the contracts it resolves
    (``Requirement.contracts()``: ``SubscriptionActive`` / ``Credits`` ->
    the entitlements contract, ``Entitled(fact)`` -> ``<owner>.facts.v1``,
    ``OrgRole`` -> the membership contract). Optional: a module tested alone
    with ``fake_access_fact`` still boots, the preflight warns when one is
    unbound, and ``sandloom add deploy`` binds them.
    """
    found: dict[str, set[str]] = {}
    seen: set[int] = set()

    def visit(dependant: Any) -> None:
        for sub in getattr(dependant, "dependencies", None) or ():
            call = getattr(sub, "call", None)
            policy = getattr(call, "__sandloom_policy__", None)
            if policy is not None and id(policy) not in seen:
                seen.add(id(policy))
                contracts = getattr(policy, "contracts", None)
                for name in contracts() if callable(contracts) else ():
                    found.setdefault(str(name), set()).add(policy.describe())
            visit(sub)

    for router in routers:
        for route in router.routes:
            visit(getattr(route, "dependant", None))
    return [
        ContractRequirement(
            name,
            required=False,
            reason="route policy: " + "; ".join(sorted(reasons)),
        )
        for name, reasons in sorted(found.items())
    ]


def _with_policy_contracts(
    consumes: list[Any], routers: Sequence[APIRouter]
) -> list[Any]:
    """``consumes`` plus the route policies' contracts it does not name yet
    (a contract the module already consumes keeps its own entry)."""
    named = {getattr(r, "name", r) for r in consumes}
    return consumes + [r for r in _policy_requirements(routers) if r.name not in named]


def _merge_consumes(
    explicit: Sequence[Any], derived: Sequence[ContractRequirement]
) -> list[Any]:
    """Explicit requirements plus derived ones, never weaker than either."""
    out: list[Any] = [
        ContractRequirement(c) if isinstance(c, str) else c for c in explicit
    ]
    by_name = {getattr(r, "name", None): i for i, r in enumerate(out)}
    for req in derived:
        index = by_name.get(req.name)
        if index is None:
            out.append(req)
            by_name[req.name] = len(out) - 1
            continue
        current = out[index]
        methods: tuple[str, ...] = ()
        if current.methods and req.methods:
            methods = tuple(sorted(set(current.methods) | set(req.methods)))
        out[index] = ContractRequirement(
            current.name,
            required=bool(current.required or req.required),
            reason=current.reason,
            methods=methods,
        )
    return out


def _routers(package: str, router: Any, found: Discovered) -> list[Any]:
    if router == AUTO:
        routers: list[Any] = list(plain_routers(package))
        for item in found.routers:
            if all(item is not r for r in routers):
                routers.append(item)
        return routers
    if router is None:
        return []
    if isinstance(router, list | tuple):
        return list(router)
    return [router]


def _register_declarations(
    core: ModuleAPI, package: str, found: Discovered, *, producers: bool
) -> None:
    """The L2 calls of the discovered declarations (spec §3 table)."""
    if producers:
        for producer in found.producers:
            core.register_event_schema(
                producer.channel, producer.__name__, event_schema(producer)
            )
    for spec in found.consumers:
        core.register_event_consumer(
            spec.event.channel,
            consumer_callback(spec),
            name=spec.name,
            durable=spec.durable,
            database=spec.database,
            required=spec.required,
            group=spec.group,
            start_from=spec.start_from,
            bind_tenant_from=spec.bind_tenant_from,
            dead_letter=spec.dead_letter,
        )
    for provider, cls in found.providers:
        core.register_service(provider.name, cls, database=provider.database)
    for saga in found.sagas:
        core.register_saga(saga)
    for projection in found.projections:
        core.register_query_projection(projection.descriptor(package))
        for consumer in projection.consumers():
            core.register_projection_consumer(consumer)
        rebuilder = projection.rebuilder()
        if rebuilder is not None:
            core.register_projection_rebuilder(projection.name, rebuilder)
        register_view_source(projection)


def define_module(
    package: str | None = None,
    /,
    *,
    version: str = "0.1.0",
    optional: bool = False,
    consumes: Sequence[Any] = (),
    capabilities: Sequence[Any] = (),
    depends_on: Sequence[str] = (),
    migrations_database: str | None = None,
    migrations_chain: str | None = None,
    health_check: Any | None = None,
    setup: Callable[[ModuleAPI], None] | None = None,
    name: str | None = None,
    router: Any = AUTO,
    events: Any = AUTO,
    issues_tokens: bool = False,
) -> Callable[[ModuleAPI], Any]:
    """Build a module's ``register(core)`` from its declarations (spec §5.7).

    In the module's ``__init__.py``::

        from sandloom.sdk import define_module

        register = define_module(__name__, version="0.1.0")

    Everything the module declares in its conventional submodules is
    discovered (routers, events, consumers, contracts, sagas, projections,
    consumed contracts). ``setup(core)`` stays for what has no declaration
    (startup tasks, claims contributors, planner operations). ``router=`` /
    ``events=`` override discovery as today; ``consumes`` adds to the derived
    requirements. Modules are REQUIRED unless ``optional=True``.
    """
    pkg = package or caller_module(1)
    if pkg == "__main__":
        raise TypeError(
            "define_module() could not infer the module package; call it as "
            "define_module(__name__, ...) from the module's __init__.py"
        )
    descriptor_name = name or pkg
    short = short_name(descriptor_name)

    def register(core: ModuleAPI) -> Any:
        found = discover(pkg)
        routers = _routers(pkg, router, found)
        schemas = (
            _event_schemas(pkg)
            if events == AUTO
            else (events if events is not None else {})
        )
        # -- validated: register (the loader applies the draft atomically) --
        for item in routers:
            core.include_router(item)
        for channel, schema in schemas.items():
            core.register_event_schema(channel=channel, name=channel, schema=schema)
        _register_declarations(core, pkg, found, producers=events == AUTO)
        if setup is not None:
            setup(core)

        core.logger.info("%s module registered", short)
        return ModuleDescriptor(
            name=descriptor_name,
            version=version,
            optional=optional,
            health_check=health_check,
            depends_on=list(depends_on),
            migrations_database=migrations_database,
            migrations_chain=migrations_chain,
            consumes=_with_policy_contracts(
                _merge_consumes(consumes, found.requirements), routers
            ),
            capabilities=[
                CapabilityRequirement(c) if isinstance(c, str) else c
                for c in capabilities
            ],
            issues_tokens=issues_tokens,
        )

    register.__qualname__ = register.__name__ = "register"
    register.__module__ = pkg
    register.__doc__ = f"Register the {short} module (declared via define_module)."
    # Read by the tooling (``sandloom test``'s generated files): an L1 module.
    register.__dict__["sandloom_l1"] = True
    return register


__all__ = [
    "Discovered",
    "define_module",
    "discover",
    "event_schema",
    "import_conventional",
    "plain_routers",
]
