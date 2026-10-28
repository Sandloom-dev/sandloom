"""The application manifest (Public API 1.0, spec §5.7; frozen at hand-off H0).

One immutable description of what an application consists of, built by the
loader at boot without I/O: per module its routes (with resolved policy),
events, consumers, contracts provided and consumed, sagas, projections,
models and migration chain, and declared route deviations. The gate, the
generated snapshots and the docs read it instead of re-deriving the same
facts from live kernel state.

Today :func:`module_manifest` builds a module's entry from what the loader
already has (the :class:`~sandloom.core.descriptors.ModuleDescriptor` and the
registration draft), and the loader records one per loaded module in
``ModuleLoadSummary.manifests``. Fields that need mechanisms of later
hand-offs are present now and filled when those land: ``RouteEntry.policy``
and ``ModuleManifest.deviations`` (model E, H1), ``EventEntry.role`` views
(typed events, H2).

Pure data: reads registration objects by attribute (duck-typed) and
performs no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sandloom.core.descriptors import ContractRequirement, ModuleDescriptor
from sandloom.core.module_api import contract_spec
from sandloom.core.orm import Base, TenantScoped

EventRole = Literal["producer", "view"]

_HTTP_METHODS_ORDER = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")


@dataclass(frozen=True)
class RouteEntry:
    """One HTTP route a module mounts."""

    module: str
    method: str
    path: str
    name: str
    #: Resolved route policy (model E: authentication, tenant binding, write
    #: defaults, risk, idempotency mode). Empty until H1 resolves it.
    policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventEntry:
    """An event channel a module registers a schema for (or views)."""

    module: str
    channel: str
    name: str
    role: EventRole = "producer"
    schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ConsumerEntry:
    """A durable event consumer and every option it was registered with."""

    module: str
    channel: str
    name: str
    durable: bool = True
    required: bool = True
    dead_letter: bool = True
    database: str = "default"
    group: str | None = None
    start_from: str = "earliest"
    bind_tenant_from: str | None = None


@dataclass(frozen=True)
class ContractEntry:
    """A sync contract a module provides."""

    module: str
    name: str
    database: str | None = None
    methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractUse:
    """A sync contract a module consumes (``consumes=``)."""

    module: str
    name: str
    required: bool = True
    methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class SagaEntry:
    """A saga a module owns."""

    module: str
    name: str
    version: int | None = None


@dataclass(frozen=True)
class ProjectionEntry:
    """A query projection a module owns."""

    module: str
    name: str
    source_channels: tuple[str, ...] = ()
    storage: str | None = None


@dataclass(frozen=True)
class ModelEntry:
    """A mapped table owned by a module."""

    module: str
    table: str
    tenant_scoped: bool


@dataclass(frozen=True)
class ChainEntry:
    """A module's migration chain."""

    module: str
    database: str | None
    chain: str | None
    path: str | None


@dataclass(frozen=True)
class DeviationEntry:
    """A declared route deviation from the defaults (``public``,
    ``no_idempotency``, ``replay_safe``, ``sensitive``, ``at_commit``,
    ``operation_intent``, ``writes``, ``allow_multi_db_write``), with its
    reason. Filled from H1 (model E)."""

    module: str
    method: str
    path: str
    kind: str
    reason: str | None = None


@dataclass(frozen=True)
class ModuleManifest:
    """Everything one module contributes to the application."""

    name: str
    version: str
    optional: bool = False
    routes: tuple[RouteEntry, ...] = ()
    events: tuple[EventEntry, ...] = ()
    consumers: tuple[ConsumerEntry, ...] = ()
    contracts_provided: tuple[ContractEntry, ...] = ()
    contracts_consumed: tuple[ContractUse, ...] = ()
    sagas: tuple[SagaEntry, ...] = ()
    projections: tuple[ProjectionEntry, ...] = ()
    models: tuple[ModelEntry, ...] = ()
    chain: ChainEntry | None = None
    deviations: tuple[DeviationEntry, ...] = ()


@dataclass(frozen=True)
class AppManifest:
    """The whole application: one :class:`ModuleManifest` per loaded module,
    in load order."""

    modules: tuple[ModuleManifest, ...] = ()

    def module(self, name: str) -> ModuleManifest:
        """The entry of module ``name`` (``KeyError`` when not loaded)."""
        for entry in self.modules:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def _flat(self, attr: str) -> Iterator[Any]:
        for entry in self.modules:
            yield from getattr(entry, attr)

    @property
    def routes(self) -> tuple[RouteEntry, ...]:
        return tuple(self._flat("routes"))

    @property
    def events(self) -> tuple[EventEntry, ...]:
        return tuple(self._flat("events"))

    @property
    def consumers(self) -> tuple[ConsumerEntry, ...]:
        return tuple(self._flat("consumers"))

    @property
    def contracts_provided(self) -> tuple[ContractEntry, ...]:
        return tuple(self._flat("contracts_provided"))

    @property
    def contracts_consumed(self) -> tuple[ContractUse, ...]:
        return tuple(self._flat("contracts_consumed"))

    @property
    def sagas(self) -> tuple[SagaEntry, ...]:
        return tuple(self._flat("sagas"))

    @property
    def projections(self) -> tuple[ProjectionEntry, ...]:
        return tuple(self._flat("projections"))

    @property
    def models(self) -> tuple[ModelEntry, ...]:
        return tuple(self._flat("models"))

    @property
    def deviations(self) -> tuple[DeviationEntry, ...]:
        return tuple(self._flat("deviations"))

    @classmethod
    def of(cls, modules: Iterable[ModuleManifest]) -> AppManifest:
        return cls(modules=tuple(modules))

    @classmethod
    def from_summary(cls, summary: Any) -> AppManifest:
        """The manifest of a load pass (``ModuleLoadSummary.manifests``),
        in load order."""
        manifests: Mapping[str, ModuleManifest] = getattr(summary, "manifests", {})
        return cls(
            modules=tuple(
                manifests[d.name] for d in summary.loaded if d.name in manifests
            )
        )


# ------------------------------------------------------------- construction


def _routes(module: str, draft: Any) -> tuple[RouteEntry, ...]:
    out: list[RouteEntry] = []
    for router, kwargs in getattr(draft, "routers", ()):
        prefix = str((kwargs or {}).get("prefix", "") or "")
        for route in getattr(router, "routes", ()):
            path = getattr(route, "path", None)
            if path is None:
                continue
            methods = sorted(
                getattr(route, "methods", None) or (),
                key=lambda m: (
                    _HTTP_METHODS_ORDER.index(m) if m in _HTTP_METHODS_ORDER else 99,
                    m,
                ),
            )
            out.extend(
                RouteEntry(
                    module=module,
                    method=str(method),
                    path=prefix + str(path),
                    name=str(getattr(route, "name", "") or ""),
                )
                for method in methods or ("*",)
            )
    return tuple(out)


def _events(module: str, draft: Any) -> tuple[EventEntry, ...]:
    out = [
        EventEntry(module=module, channel=str(channel), name=str(name), schema=schema)
        for channel, name, schema in getattr(draft, "event_schemas", ())
    ]
    for obj in getattr(draft, "event_schema_objs", ()):
        channel = getattr(obj, "channel", None)
        if channel is None:
            continue
        out.append(
            EventEntry(
                module=module,
                channel=str(channel),
                name=str(getattr(obj, "name", "") or channel),
                schema=getattr(obj, "schema", None),
            )
        )
    return tuple(out)


def _consumers(module: str, draft: Any) -> tuple[ConsumerEntry, ...]:
    return tuple(
        ConsumerEntry(
            module=module,
            channel=str(getattr(d, "channel", "")),
            name=str(getattr(d, "name", "")),
            durable=bool(getattr(d, "durable", True)),
            required=bool(getattr(d, "required", True)),
            dead_letter=bool(getattr(d, "dead_letter", True)),
            database=str(getattr(d, "database", "default")),
            group=getattr(d, "group", None),
            start_from=str(getattr(d, "start_from", "earliest")),
            bind_tenant_from=getattr(d, "bind_tenant_from", None),
        )
        for d in getattr(draft, "event_consumers", ())
    )


def _contract_methods(impl: Any) -> tuple[str, ...]:
    return tuple(
        attr
        for attr in sorted(dir(impl))
        if not attr.startswith("_")
        and contract_spec(getattr(impl, attr, None)) is not None
    )


def _provided(module: str, draft: Any) -> tuple[ContractEntry, ...]:
    databases: Mapping[str, str] = getattr(draft, "service_databases", {}) or {}
    return tuple(
        ContractEntry(
            module=module,
            name=str(name),
            database=databases.get(name),
            methods=_contract_methods(impl),
        )
        for name, impl in getattr(draft, "services", ())
    )


def _consumed(
    module: str, consumes: Sequence[ContractRequirement | str]
) -> tuple[ContractUse, ...]:
    out: list[ContractUse] = []
    for req in consumes:
        if isinstance(req, str):
            out.append(ContractUse(module=module, name=req))
        else:
            out.append(
                ContractUse(
                    module=module,
                    name=req.name,
                    required=bool(req.required),
                    methods=tuple(req.methods),
                )
            )
    return tuple(out)


def _sagas(module: str, draft: Any) -> tuple[SagaEntry, ...]:
    return tuple(
        SagaEntry(
            module=module,
            name=str(getattr(s, "name", "")),
            version=getattr(s, "version", None),
        )
        for s in getattr(draft, "sagas", ())
    )


def _projections(module: str, draft: Any) -> tuple[ProjectionEntry, ...]:
    return tuple(
        ProjectionEntry(
            module=module,
            name=str(getattr(p, "name", "")),
            source_channels=tuple(getattr(p, "source_events", ()) or ()),
            storage=(
                str(getattr(p, "storage", None))
                if getattr(p, "storage", None) is not None
                else None
            ),
        )
        for p in getattr(draft, "query_projections", ())
    )


def _models(module: str, package: str | None) -> tuple[ModelEntry, ...]:
    if not package:
        return ()
    out: list[ModelEntry] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        owner = getattr(cls, "__module__", "")
        if owner != package and not owner.startswith(package + "."):
            continue
        table = getattr(cls, "__tablename__", None)
        if not isinstance(table, str):
            continue
        out.append(
            ModelEntry(
                module=module,
                table=table,
                tenant_scoped=issubclass(cls, TenantScoped),
            )
        )
    return tuple(sorted(out, key=lambda m: m.table))


def module_manifest(
    descriptor: ModuleDescriptor,
    draft: Any = None,
    *,
    package: str | None = None,
) -> ModuleManifest:
    """The manifest entry of one module from what the loader has.

    ``draft`` is the module's ``ModuleRegistrationDraft`` (its buffered
    registrations); ``package`` the import name, for the models it owns.
    No I/O: routers, schemas and descriptors are read, never executed.
    """
    name = descriptor.name
    return ModuleManifest(
        name=name,
        version=str(descriptor.version),
        optional=bool(descriptor.optional),
        routes=_routes(name, draft),
        events=_events(name, draft),
        consumers=_consumers(name, draft),
        contracts_provided=_provided(name, draft),
        contracts_consumed=_consumed(name, descriptor.consumes),
        sagas=_sagas(name, draft),
        projections=_projections(name, draft),
        models=_models(name, package),
        chain=ChainEntry(
            module=name,
            database=descriptor.migrations_database,
            chain=descriptor.migrations_chain,
            path=descriptor.migrations_path,
        ),
    )


__all__ = [
    "AppManifest",
    "ChainEntry",
    "ConsumerEntry",
    "ContractEntry",
    "ContractUse",
    "DeviationEntry",
    "EventEntry",
    "EventRole",
    "ModelEntry",
    "ModuleManifest",
    "ProjectionEntry",
    "RouteEntry",
    "SagaEntry",
    "module_manifest",
]
