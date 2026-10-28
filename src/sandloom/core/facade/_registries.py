"""Infrastructure, schema, projection and consumer registries of the
:class:`CoreFacade`.

Capability resolution by logical name (``capability`` / ``database`` /
``cache`` / ...), the Unit-of-Work factories, the event-schema registry, the
query-projection registry, the generic event consumers, the registration
snapshot / restore a failed module load rolls back to, and the read-only
:meth:`CoreFacade.runtime_view`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import sandloom.core.projections.operations as _projection_ops
from sandloom.core.contracts import (
    EventSchemaObject,
    SchemaEntry,
    SchemaRegistryContract,
)
from sandloom.core.facade._runtime import (
    CoreRuntime,
)
from sandloom.core.facade._state import (
    _FacadeState,
)
from sandloom.core.planner import DECLARED_OPERATIONS_ATTR
from sandloom.core.projections.registry import QueryProjectionRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade


class _RegistriesMixin(_FacadeState):
    """CoreFacade methods: see :mod:`sandloom.core.facade._registries`."""

    # -----------------
    # Infrastructure registry (Wave 6)
    # -----------------
    def set_infra(self, registry: Any) -> None:
        """Bind the infrastructure registry this application owns."""
        self._infra = registry

    @property
    def infra(self) -> Any:
        """Return the bound InfraRegistry (raises if not wired)."""
        if self._infra is None:
            raise RuntimeError("Infrastructure registry not configured")
        return self._infra

    def capability(
        self, capability: str, name: str = "default", contract: type | None = None
    ) -> Any:
        """Resolve any capability instance by logical name.

        The universal accessor behind the capability runtime: ``cache``,
        ``database``, ``graph_store``, ``workflow`` and any plugin-registered
        capability go through here. The typed sugar methods below are thin
        wrappers over this call.
        """
        return self.infra.resolve(capability, name, contract=contract)

    def require_capability(
        self,
        capability: str,
        name: str = "default",
        *,
        features: set[str] | None = None,
        contract: type | None = None,
    ) -> Any:
        """Resolve a capability, requiring the given features.

        Raises a diagnostic :class:`FeatureUnsupportedError` if the configured
        driver cannot provide a required feature — so a module never silently
        runs against a backend that lacks what it needs.
        """
        return self.infra.resolve(
            capability,
            name,
            features=frozenset(features or ()),
            contract=contract,
        )

    def database(self, name: str = "default") -> Any:
        """Return the DatabaseProvider for a named instance.

        Modules address databases by logical name only — never by engine,
        driver, or connection string. Multiple databases can coexist.
        """
        return self.capability("database", name)

    def cache(self, name: str = "default") -> Any:
        """Return the CacheProvider for a named instance."""
        return self.capability("cache", name)

    def objectstore(self, name: str = "default") -> Any:
        """Return the ObjectStoreProvider for a named instance."""
        return self.capability("objectstore", name)

    def taskqueue(self, name: str = "default") -> Any:
        """Return the TaskQueueProvider for a named instance."""
        return self.capability("taskqueue", name)

    def eventbus_provider(self, name: str = "default") -> Any:
        """Return the EventBusProvider for a named instance."""
        return self.capability("eventbus", name)

    def notifier(self, name: str = "default") -> Any:
        """Return the NotifierProvider for a named instance."""
        return self.capability("notification", name)

    # -----------------
    # Unit of Work factory
    # -----------------
    def register_uow_factory(self, factory: Callable[[], Any]) -> None:
        """Reject the removed out-of-registry UoW wiring path.

        Kept as a fail-closed compatibility tombstone so existing integrations
        receive an actionable error instead of an ``AttributeError``.
        """
        raise RuntimeError(
            "register_uow_factory() is no longer supported; configure a database "
            "driver through the infrastructure registry"
        )

    def get_uow_factory(self, name: str = "default") -> Callable[[], Any]:
        """Return a UoW factory for a named database instance.

        The infrastructure registry is the only source of database providers.
        Missing registry/configuration and provider errors propagate rather than
        selecting an implicit default database.
        """
        factory: Callable[[], Any] = self.database(name).uow_factory()
        return factory

    def get_contract_uow_factory(self, name: str = "default") -> Callable[[], Any]:
        """UoW factory for a contract call's own Unit of Work on database ``name``.

        Served from the provider's DEDICATED contract pool when the database
        driver offers one (``contract_uow_factory``), so an in-process contract
        call (or claims contributor) never waits for a connection held by the
        request that made it; otherwise the instance's regular factory.
        """
        provider = self.database(name)
        contract_factory = getattr(provider, "contract_uow_factory", None)
        if callable(contract_factory):
            return cast(Callable[[], Any], contract_factory())
        return self.get_uow_factory(name)

    def register_schema_registry(self, registry: SchemaRegistryContract) -> None:
        """Register the schema registry instance."""
        self._schema_registry = registry

    def get_schema_registry(self) -> SchemaRegistryContract:
        """Returns the schema registry service."""
        if self._schema_registry is None:
            raise RuntimeError("SchemaRegistry has not been registered in CoreFacade.")
        return self._schema_registry

    def register_event_schema(
        self, channel: str, name: str, schema: dict[str, Any] | None = None
    ) -> SchemaEntry:
        """Registers an event schema by passing its components."""
        return self.get_schema_registry().register(channel, name, schema)

    def get_event_schema(self, channel: str) -> SchemaEntry | None:
        """Gets the registered schema by channel name."""
        return self.get_schema_registry().get(channel)

    def register_event_schema_obj(self, event_schema: EventSchemaObject) -> SchemaEntry:
        """Register an EventSchema instance (helper).

        Accepts object with attributes: channel, name, schema.
        """
        registry = self.get_schema_registry()
        channel = event_schema.channel
        name = event_schema.name
        schema = event_schema.schema
        return registry.register(channel, name, schema)

    def list_event_schemas(self) -> list[Any]:
        """Return list of registered event schemas (read-only snapshot).

        Useful for diagnostics, documentation endpoints or exporting schema
        inventory. Returns empty list if registry unavailable.
        """
        return self.get_schema_registry().list()

    # -----------------
    # Query projections (Cross-Module Query layer)
    # -----------------
    @property
    def projection_registry(self) -> QueryProjectionRegistry:
        """The cross-module query/projection registry (always available)."""
        return self._projection_registry

    def register_query_projection(self, descriptor: Any) -> None:
        """Register a denormalised cross-module read model owned by a module."""
        self._projection_registry.register_projection(descriptor)

    def register_projection_consumer(self, descriptor: Any) -> None:
        """Register a durable consumer that applies a source channel to it.

        The consumer of a projection that purges tombstones refuses an event
        older than its purge watermark (spec §6.4 item 5)."""
        projection = self._projection_registry.get_projection(
            getattr(descriptor, "projection", "")
        )
        if getattr(projection, "purge", None) is not None:
            descriptor = _projection_ops.watermark_guarded(descriptor)
        self._projection_registry.register_consumer(descriptor)

    def register_projection_rebuilder(self, projection: str, rebuilder: Any) -> None:
        """Register the rebuilder that reconstructs ``projection`` from source."""
        self._projection_registry.register_rebuilder(projection, rebuilder)

    # -----------------
    # Generic durable event consumers
    # -----------------
    @property
    def event_consumers(self) -> list[Any]:
        """Registered generic event consumers (read-only snapshot)."""
        return list(self._event_consumers)

    def register_event_consumer(self, descriptor: Any) -> None:
        """Register a durable consumer that reacts to an event in its own UoW."""
        self._event_consumers.append(descriptor)

    # -----------------
    # Saga definitions (ADR-0008)
    # -----------------
    @property
    def sagas(self) -> list[tuple[Any, str]]:
        """Registered ``(definition, module)`` pairs (read-only snapshot)."""
        return list(self._sagas)

    def register_saga(self, definition: Any, module: str | None = None) -> None:
        """Store a saga definition and bind it to its module.

        Duck-typed (the facade never imports ``sandloom.core.sagas``): the
        definition's ``bind(module)`` sets its ``<module>.<name>`` type; the
        runtime checks it against its contracts at install time.
        """
        owner = module or self._registering_module
        if not owner:
            raise ValueError(
                "register_saga needs the owning module: call it from a module's "
                "setup (core.register_saga) or pass module="
            )
        saga_type = definition.bind(owner)
        for known, _owner in self._sagas:
            if known is definition:
                return
            if known.saga_type == saga_type and known.version == definition.version:
                raise ValueError(
                    f"saga {saga_type} v{definition.version} registered twice"
                )
        self._sagas.append((definition, owner))

    def snapshot_registrations(self) -> dict[str, Any]:
        """Capture all module-registerable kernel state for a later restore.

        Lets :meth:`ModuleRegistrationDraft.apply` be truly atomic: if any
        applied step raises, :meth:`restore_registrations` rewinds every
        collection a module can mutate (services, lifecycle hooks, health
        checks, claims contributors, routes and — if the schema registry
        supports it — event schemas) back to this exact point. Captured by
        position/identity, so it only ever rewinds THIS module's additions
        (module loading is sequential).
        """
        registry = self._schema_registry
        schemas = (
            registry.snapshot()
            if registry is not None and hasattr(registry, "snapshot")
            else None
        )
        return {
            "startup": len(self._startup_tasks),
            "startup_critical": len(self._startup_critical),
            "startup_phase": len(self._startup_phase),
            "shutdown": len(self._shutdown_tasks),
            "health": dict(self._health_checks),
            "claims": len(self._claims_contributors),
            "claims_owner_modules": dict(self._claims_owner_modules),
            "services": set(self.service_registry.list()),
            "routes": len(self.app.router.routes),
            "schemas": schemas,
            "projections": self._projection_registry.snapshot(),
            "event_consumers": len(self._event_consumers),
            "sagas": len(self._sagas),
            "access_facts": set(self._access_facts),
            # planner.register_prepared's record of the declared operations
            "declared_operations": dict(
                self.__dict__.get(DECLARED_OPERATIONS_ATTR) or {}
            ),
        }

    def restore_registrations(self, snapshot: dict[str, Any]) -> None:
        """Rewind kernel state to a :meth:`snapshot_registrations` checkpoint."""
        del self._startup_tasks[snapshot["startup"] :]
        del self._startup_critical[snapshot["startup_critical"] :]
        del self._startup_phase[snapshot.get("startup_phase", snapshot["startup"]) :]
        del self._shutdown_tasks[snapshot["shutdown"] :]
        self._health_checks.clear()
        self._health_checks.update(snapshot["health"])
        del self._claims_contributors[snapshot["claims"] :]
        self._claims_owner_modules.clear()
        self._claims_owner_modules.update(snapshot.get("claims_owner_modules", {}))
        kept_services: set[str] = snapshot["services"]
        for name in self.service_registry.list():
            if name not in kept_services:
                self.service_registry.remove(name)
                self._service_owners.pop(name, None)
                self._service_databases.pop(name, None)
                self._bound_contracts.pop(name, None)
        # Routers are applied last and only append, so truncating to the
        # captured length removes exactly the routes this module added.
        del self.app.router.routes[snapshot["routes"] :]
        # Invalidate the cached OpenAPI schema so it regenerates without the
        # rolled-back routes.
        self.app.openapi_schema = None
        registry = self._schema_registry
        if (
            snapshot["schemas"] is not None
            and registry is not None
            and hasattr(registry, "restore")
        ):
            registry.restore(snapshot["schemas"])
        self._restore_declarations(snapshot)

    def _restore_declarations(self, snapshot: dict[str, Any]) -> None:
        """The declarative part of :meth:`restore_registrations`: projections,
        consumers, sagas, access facts and declared operations."""
        projections = snapshot.get("projections")
        if projections is not None:
            self._projection_registry.restore(projections)
        event_consumers = snapshot.get("event_consumers")
        if event_consumers is not None:
            del self._event_consumers[event_consumers:]
        sagas = snapshot.get("sagas")
        if sagas is not None:
            del self._sagas[sagas:]
        kept_facts = snapshot.get("access_facts")
        if kept_facts is not None:
            for name in list(self._access_facts):
                if name not in kept_facts:
                    del self._access_facts[name]
        declared = snapshot.get("declared_operations")
        if declared is not None:
            if declared:
                self.__dict__[DECLARED_OPERATIONS_ATTR] = dict(declared)
            else:
                self.__dict__.pop(DECLARED_OPERATIONS_ATTR, None)

    # -----------------
    # Read-only runtime view
    # -----------------
    def runtime_view(self) -> CoreRuntime:
        """The read-only :class:`CoreRuntime` view handed to module code."""
        view = self.__dict__.get("_runtime_view")
        if view is None:
            view = CoreRuntime(cast("CoreFacade", self))
            self.__dict__["_runtime_view"] = view
        return view
