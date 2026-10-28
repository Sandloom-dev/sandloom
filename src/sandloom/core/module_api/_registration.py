"""``ModuleAPI`` — the registration surface handed to ``register(core)`` —
and the transactional :class:`ModuleRegistrationDraft` behind it."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

import sandloom.core.consumers.callbacks as _callbacks
import sandloom.core.contracts.validators as _validators
import sandloom.core.descriptors as _descriptors
import sandloom.core.planner as _planner
import sandloom.core.route_markers as _route_markers
from sandloom.core import contract_wire
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.module_api._provider import contract_schema
from sandloom.core.module_api._service_registry import (
    ServiceRegistryError,
    _validate_service_name,
)


class _RegistrationControlFlow(Exception):
    """An expected loader deferral/duplicate, not a failed registration."""


if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import APIRouter

    from sandloom.core.config.settings import Settings
    from sandloom.core.descriptors import HealthCheck
    from sandloom.core.facade import CoreFacade, ShutdownTask, StartupTask


class ModuleRegistrationConflict(RuntimeError):
    """A buffered registration would conflict with live kernel state.

    Raised by :meth:`ModuleRegistrationDraft.preflight` BEFORE anything is
    applied, so a conflicting module leaves the kernel completely untouched.
    """


class ModuleRegistrationDraft:
    """A buffer of one module's registrations, applied atomically on commit.

    Module loading is transactional: instead of mutating the kernel as
    ``register()`` runs (then snapshotting/restoring private kernel state if it
    fails — fragile and tied to FastAPI/ServiceRegistry internals), every write
    a module makes is appended here. ``commit`` first :meth:`preflight`s the
    whole draft against live kernel state (duplicate services / health checks,
    breaking or conflicting event schemas, malformed names) and only then
    :meth:`apply`s it. If ``register()`` raises first, commit never runs; if
    preflight fails, NOTHING is applied. A half-registered module can no longer
    leave an orphan service or schema in the live inventory.
    """

    def __init__(self) -> None:
        self.routers: list[tuple[Any, dict[str, Any]]] = []
        self.services: list[tuple[str, Any]] = []
        self.service_databases: dict[str, str] = {}
        self.startup: list[tuple[Any, bool]] = []
        self.shutdown: list[Any] = []
        self.health: list[tuple[str, Any]] = []
        self.event_schemas: list[tuple[str, str, dict[str, Any] | None]] = []
        self.event_schema_objs: list[Any] = []
        self.claims_contributors: list[tuple[Any, bool]] = []
        self.query_projections: list[Any] = []
        self.projection_consumers: list[Any] = []
        self.projection_rebuilders: list[tuple[str, Any]] = []
        self.event_consumers: list[Any] = []
        self.access_facts: list[Any] = []
        self.sagas: list[Any] = []

    def commit(self, facade: CoreFacade) -> None:
        """Validate the whole draft, then apply it (atomic: all or nothing)."""
        self.preflight(facade)
        self.apply(facade)

    def preflight(self, facade: CoreFacade) -> None:
        """Check the draft against live state; raise before applying anything."""
        errors: list[str] = []
        self._preflight_services(facade, errors)
        self._preflight_health(facade, errors)
        self._preflight_schemas(facade, errors)
        self._preflight_projections(facade, errors)
        self._preflight_sagas(facade, errors)
        if errors:
            raise ModuleRegistrationConflict("; ".join(errors))

    def apply(self, facade: CoreFacade) -> None:  # noqa: C901
        """Apply every buffered registration to ``facade`` (routers last).

        Runs only after :meth:`preflight` passed, so the validated operations
        (services, schemas, health) should not fail partway. As a strict
        all-or-nothing backstop the whole apply is wrapped in a snapshot/restore:
        if ANY step still raises (e.g. ``include_router`` hits a FastAPI route
        conflict preflight cannot foresee), the kernel is rewound to exactly its
        pre-apply state so a half-registered module leaves no orphan service,
        route or schema behind.
        """
        checkpoint = facade.snapshot_registrations()
        try:
            for svc_name, impl in self.services:
                facade.register_service(
                    svc_name, impl, database=self.service_databases.get(svc_name)
                )
            for task, critical in self.startup:
                facade.add_startup_task(task, critical=critical)
            for shutdown_task in self.shutdown:
                facade.add_shutdown_task(shutdown_task)
            for hc_name, fn in self.health:
                facade.register_health_check(hc_name, fn)
            for contributor, critical in self.claims_contributors:
                facade.add_claims_contributor(contributor, critical=critical)
            for channel, schema_name, schema in self.event_schemas:
                facade.register_event_schema(channel, schema_name, schema)
            for obj in self.event_schema_objs:
                facade.register_event_schema_obj(obj)
            for projection in self.query_projections:
                facade.register_query_projection(projection)
            module = _registering_module(facade)
            for consumer in self.projection_consumers:
                facade.register_projection_consumer(_stamp_module(consumer, module))
            for proj_name, rebuilder in self.projection_rebuilders:
                facade.register_projection_rebuilder(proj_name, rebuilder)
            for event_consumer in self.event_consumers:
                facade.register_event_consumer(_stamp_module(event_consumer, module))
            for fact in self.access_facts:
                facade.register_access_fact(fact)
            for saga in self.sagas:
                facade.register_saga(saga, module)
            for router, kwargs in self.routers:
                before = len(facade.app.router.routes)
                facade.app.include_router(router, **kwargs)
                if module is not None:
                    _route_markers.stamp_routes(
                        facade.app.router.routes[before:], module
                    )
        except Exception:
            facade.restore_registrations(checkpoint)
            raise

    # -- preflight helpers ------------------------------------------------
    def _preflight_services(self, facade: CoreFacade, errors: list[str]) -> None:
        seen: set[str] = set()
        for name, impl in self.services:
            try:
                _validate_service_name(name)
            except ServiceRegistryError as exc:
                errors.append(f"invalid service name {name!r}: {exc}")
            try:
                contract_schema(impl)
            except contract_wire.ContractTypeError as exc:
                errors.append(f"service {name!r}: {exc}")
            errors.extend(facade.contract_dto_problems(name, impl))
            if name in seen or facade.service_registry.contains(name):
                errors.append(f"duplicate service {name!r}")
            seen.add(name)

    def _preflight_health(self, facade: CoreFacade, errors: list[str]) -> None:
        live = facade.health_checks
        seen: set[str] = set()
        for name, _fn in self.health:
            if name in seen or name in live:
                errors.append(f"duplicate health check {name!r}")
            seen.add(name)

    def _preflight_schemas(self, facade: CoreFacade, errors: list[str]) -> None:
        try:
            registry = facade.get_schema_registry()
        except Exception:  # noqa: BLE001 - no registry/validators: skip schema checks
            return

        # Validate the object-form schemas (register_event_schema_obj) on the
        # SAME footing as the tuple-form ones: apply() calls register for both,
        # so an unvalidated obj could raise mid-apply and strand earlier writes.
        obj_schemas = [
            (obj.channel, getattr(obj, "name", None), obj.schema)
            for obj in self.event_schema_objs
        ]

        seen: dict[str, dict[str, Any] | None] = {}
        for channel, _name, schema in [*self.event_schemas, *obj_schemas]:
            try:
                _validators.validate_channel_name(channel)
            except Exception as exc:  # noqa: BLE001 - collected as a registration error
                errors.append(f"invalid event channel {channel!r}: {exc}")
                continue
            # Conflict within the draft itself.
            prior = seen.get(channel)
            if (
                prior
                and schema
                and prior != schema
                and _validators.classify_schema_change(
                    prior, schema, channel, 1
                ).breaking
            ):
                errors.append(f"conflicting schema for {channel!r} within module")
            seen[channel] = schema
            # Breaking change vs the live registry.
            existing = registry.get(channel)
            old = getattr(existing, "schema", None)
            if (
                old
                and schema
                and old != schema
                and _validators.classify_schema_change(old, schema, channel, 1).breaking
            ):
                errors.append(f"breaking schema change for {channel!r}")

    def _preflight_sagas(self, facade: CoreFacade, errors: list[str]) -> None:
        """Duplicate ``(name, version)`` within the draft or vs live state."""
        module = _registering_module(facade)
        short = _module_name(module or "")
        live = {(s.saga_type, s.version) for s, _owner in facade.sagas}
        seen: set[tuple[str, int]] = set()
        for saga in self.sagas:
            key = (f"{short}.{saga.name}", saga.version)
            if key in seen or (
                key in live and all(s is not saga for s, _ in facade.sagas)
            ):
                errors.append(f"duplicate saga {key[0]!r} v{key[1]}")
            seen.add(key)

    def _preflight_projections(self, facade: CoreFacade, errors: list[str]) -> None:
        """Validate projection/consumer registrations against live + draft state.

        Catches duplicate projection or consumer names (vs the live registry and
        within the draft) and consumers that feed an unknown projection — a
        misdeclared read module fails the load instead of silently consuming
        into nowhere.
        """
        registry = facade.projection_registry
        draft_projections: set[str] = set()
        for projection in self.query_projections:
            name = projection.name
            if name in draft_projections or registry.get_projection(name) is not None:
                errors.append(f"duplicate query projection {name!r}")
            draft_projections.add(name)

        known = draft_projections | {p.name for p in registry.list_projections()}
        # The effective consumer GROUP (``<module>:<name>``, or an explicit
        # ``group=``) is the durable consumer-group id and the exactly-once
        # inbox key — unique across BOTH projection consumers and generic
        # event consumers (live + draft). Two modules may reuse a ``name``;
        # one module may not register the same group twice.
        module = _registering_module(facade)
        live_groups = {_group_of(c) for c in registry.list_consumers()} | {
            _group_of(c) for c in facade.event_consumers
        }
        seen_groups: set[str] = set()
        for consumer in self.projection_consumers:
            cname = _group_of(_stamp_module(consumer, module))
            if cname in seen_groups or cname in live_groups:
                errors.append(f"duplicate consumer {cname!r}")
            seen_groups.add(cname)
            if consumer.projection not in known:
                errors.append(
                    f"projection consumer {consumer.name!r} feeds unknown "
                    f"projection {consumer.projection!r}"
                )
        for consumer in self.event_consumers:
            cname = _group_of(_stamp_module(consumer, module))
            if cname in seen_groups or cname in live_groups:
                errors.append(f"duplicate consumer {cname!r}")
            seen_groups.add(cname)

        for proj_name, _rebuilder in self.projection_rebuilders:
            if proj_name not in known:
                errors.append(
                    f"rebuilder registered for unknown projection {proj_name!r}"
                )


def pending_saga_requirements(api: ModuleAPI) -> list[Any]:
    """The consumed-contract requirements of the sagas ``api`` buffered.

    The loader merges them into the module's descriptor (``consumes``) before
    the consumed-contract preflight.
    """
    draft = api._draft
    if draft is None:
        return []
    found: list[Any] = []
    for saga in draft.sagas:
        found.extend(_descriptors.saga_contract_requirements(saga))
    return found


def _registering_module(facade: Any) -> str | None:
    """The module whose draft is being committed (set by the loader)."""
    name = getattr(facade, "_registering_module", None)
    return str(name) if name else None


def _stamp_module(descriptor: Any, module: str | None) -> Any:
    """Attribute a consumer descriptor to its registering module.

    The module namespaces the consumer's durable group (``<module>:<name>``)
    so equally named consumers of different modules never share one group.
    A descriptor that already names its module is left untouched.
    """
    if module is None or getattr(descriptor, "module", "absent") is not None:
        return descriptor
    try:
        return dataclasses.replace(descriptor, module=module)
    except TypeError:  # pragma: no cover - non-dataclass third-party descriptor
        return descriptor


def _group_of(descriptor: Any) -> str:
    return str(getattr(descriptor, "group_name", None) or descriptor.name)


class ModuleAPI:
    """Stable, minimal registration surface for business modules.

    When constructed with a ``draft`` (the loader's transactional path) write
    methods buffer into it and are applied only on a successful commit; read
    methods always reflect the live facade. Without a draft (direct use in
    tests) writes apply to the facade immediately, preserving the simple API.
    """

    def __init__(
        self,
        facade: CoreFacade,
        *,
        draft: ModuleRegistrationDraft | None = None,
    ) -> None:
        self._facade = facade
        self._draft = draft

    # -- HTTP -------------------------------------------------------------
    def include_router(self, router: APIRouter, **kwargs: Any) -> None:
        """Mount a module's router (replaces reaching into ``core.app``)."""
        if self._draft is not None:
            self._draft.routers.append((router, kwargs))
        else:
            self._facade.app.include_router(router, **kwargs)

    # -- Sync service contracts ------------------------------------------
    def register_service(
        self, name: str, impl: Any, *, database: str | None = None
    ) -> None:
        """Register a sync service contract provided by this module.

        ``database`` overrides the database the contract runtime opens the
        provider's Unit of Work on; by default it is the database the module's
        descriptor declares (``ModuleDescriptor.database_instances()``).
        """
        if self._draft is not None:
            self._draft.services.append((name, impl))
            if database is not None:
                self._draft.service_databases[name] = database
        else:
            self._facade.register_service(name, impl, database=database)

    def get_service(self, name: str) -> Any:
        if self._draft is not None:
            raise ModuleRegistrationConflict(
                "service resolution during module registration is forbidden; "
                "resolve contracts at request/startup time after all modules load"
            )
        return self._facade.get_service(name)

    # -- Event schemas / contracts ---------------------------------------
    def register_event_schema(
        self, channel: str, name: str, schema: dict[str, Any] | None = None
    ) -> Any:
        if self._draft is not None:
            self._draft.event_schemas.append((channel, name, schema))
            return None
        return self._facade.register_event_schema(channel, name, schema)

    def register_event_schema_obj(self, event_schema: Any) -> Any:
        if self._draft is not None:
            self._draft.event_schema_objs.append(event_schema)
            return None
        return self._facade.register_event_schema_obj(event_schema)

    def get_schema_registry(self) -> Any:
        return self._facade.get_schema_registry()

    # -- Lifecycle / health ----------------------------------------------
    def add_startup_task(self, task: StartupTask, *, critical: bool = False) -> None:
        if self._draft is not None:
            self._draft.startup.append((task, critical))
        else:
            self._facade.add_startup_task(task, critical=critical)

    def add_shutdown_task(self, task: ShutdownTask) -> None:
        if self._draft is not None:
            self._draft.shutdown.append(task)
        else:
            self._facade.add_shutdown_task(task)

    def register_health_check(self, name: str, fn: HealthCheck) -> None:
        if self._draft is not None:
            self._draft.health.append((name, fn))
        else:
            self._facade.register_health_check(name, fn)

    # -- Token claims -----------------------------------------------------
    def add_claims_contributor(self, fn: Any, *, critical: bool = False) -> None:
        """Register a contributor of JWT claims (``async (user_id) -> dict``).

        The contributor runs in the module's OWN Unit of Work on its own
        database (read it with :func:`contract_session`); it is also exposed as
        the ``<module>.claims.v1`` contract so a token issuer in another process
        can collect it over RPC (``SERVICE_BINDINGS``). ``critical=True``
        aborts token minting (503) if the contributor fails (use for security
        claims like roles/permissions and tenant allowlists).
        """
        if self._draft is not None:
            self._draft.claims_contributors.append((fn, critical))
        else:
            self._facade.add_claims_contributor(fn, critical=critical)

    # -- Query projections (Cross-Module Query layer) ---------------------
    def register_query_projection(self, descriptor: Any) -> None:
        """Declare a denormalised cross-module read model this module owns."""
        if self._draft is not None:
            self._draft.query_projections.append(descriptor)
        else:
            self._facade.register_query_projection(descriptor)

    def register_projection_consumer(self, descriptor: Any) -> None:
        """Declare a durable consumer that applies a source channel to a projection."""
        if self._draft is not None:
            self._draft.projection_consumers.append(descriptor)
        else:
            self._facade.register_projection_consumer(descriptor)

    def register_projection_rebuilder(self, projection: str, rebuilder: Any) -> None:
        """Register the rebuilder that reconstructs ``projection`` from source."""
        if self._draft is not None:
            self._draft.projection_rebuilders.append((projection, rebuilder))
        else:
            self._facade.register_projection_rebuilder(projection, rebuilder)

    # -- Durable event consumers (react to an event in your own UoW) ------
    def register_event_consumer(
        self,
        channel: str,
        handler: Any,
        *,
        name: str,
        durable: bool = True,
        database: str = "default",
        required: bool = True,
        group: str | None = None,
        start_from: str = "earliest",
        bind_tenant_from: str | None = None,
        dead_letter: bool = True,
    ) -> None:
        """Subscribe ``handler`` to ``channel`` as a durable consumer.

        The sanctioned "react to another module's event with a side effect"
        path: at startup the framework subscribes the handler under its own
        durable consumer group ``<module>:<name>`` (namespaced by the
        registering module, so two modules — or this module deployed in two
        services — never share a group and split its events) and runs each
        delivery in a Unit of Work on ``database`` (commit on success,
        rollback+redelivery on error). With ``durable`` (default) the event id
        is claimed for exactly-once apply. ``required=True`` (default) gates
        application readiness on the live subscription; mark deliberately
        best-effort consumers optional.

        ``group`` pins an explicit group id instead (e.g. the bare pre-0.4
        name, to keep draining an existing group). ``start_from`` applies when
        the group is first created: ``"earliest"`` (default; every event still
        within ``EVENT_STREAM_RETENTION_SECONDS``), ``"latest"`` or an ISO-8601
        timestamp. ``bind_tenant_from`` (opt-in) names the payload field holding
        the tenant id: the handler then runs with that tenant bound, so
        ``TenantScoped`` code works; a missing/invalid value fails the delivery.
        The handler signature is ``async (session, envelope) -> None``.
        ``dead_letter=False`` retries a failing delivery FOREVER (capped
        backoff) instead of dead-lettering it after the delivery budget — for
        effects that must never be silently parked; the handler raises
        ``PoisonEventError`` (``sandloom.core.di``) for an event that can never
        apply, which is dead-lettered at once with a CRITICAL log.
        """

        descriptor = _callbacks.EventConsumerDescriptor(
            name=name,
            channel=channel,
            handler=handler,
            durable=durable,
            database=database,
            required=required,
            group=group,
            start_from=start_from,
            bind_tenant_from=bind_tenant_from,
            dead_letter=dead_letter,
        )
        if self._draft is not None:
            self._draft.event_consumers.append(descriptor)
        else:
            self._facade.register_event_consumer(descriptor)

    # -- Sagas (ADR-0008) --------------------------------------------------
    def register_saga(self, definition: Any) -> None:
        """Register a saga this module owns (``sandloom.core.module_api.Saga``).

        Binds it to the module (``saga_type = "<module>.<name>"``) and adds a
        ``ContractRequirement(name, methods=...)`` for every contract its
        steps call to the module's ``consumes`` — a missing provider is then a
        ``ContractBindingError`` at startup. The kernel checks the calls
        against the providers' describe documents once every module loaded
        and installs the advance consumer ``<module>:sagas@advance`` and the
        timer sweep.
        """
        if not isinstance(definition, _descriptors.Saga):
            raise TypeError(
                f"register_saga expects a Saga definition, got {definition!r}"
            )
        if self._draft is not None:
            self._draft.sagas.append(definition)
        else:
            self._facade.register_saga(definition)

    # -- Access facts (authorization subsystem) --------------------------
    def register_access_fact(self, fact: Any) -> None:
        """Register an :class:`~sandloom.shared.authz.facts.AccessFact`.

        Binds a fact name to its claim/projection/live resolution paths so
        ``Entitled(name)`` policies resolve it by configuration.
        """
        if self._draft is not None:
            self._draft.access_facts.append(fact)
        else:
            self._facade.register_access_fact(fact)

    # -- Declared operations (execution planner) ------------------------
    def register_operations(self, *operations: Any) -> None:
        """Declare this module's planner operations (``ReadOperation`` /
        ``JoinRead``): each is planned at startup (a ``critical`` startup task,
        so a declaration that does not match the schema fails the boot), the
        chosen executor and the reason are logged, and ``/diagnostics/core``
        lists them (``planner``)."""

        ops = tuple(operations)
        for op in ops:
            if not isinstance(op, _planner.ReadOperation):
                raise TypeError(f"not a declared operation: {op!r}")
        if not ops:
            return
        facade = self._facade
        # the registering module (set while a module registers); fall back to
        # the package of the first operation's entity
        module = facade._registering_module or ".".join(
            getattr(ops[0].entity, "__module__", "?").split(".")[:2]
        )

        async def prepare_declared_operations() -> None:
            _planner.register_prepared(facade, module, ops)

        self.add_startup_task(prepare_declared_operations, critical=True)

    # -- Logging / config -------------------------------------------------
    def logger_factory(self, name: str) -> logging.Logger:
        return self._facade.logger_factory(name)

    @property
    def logger(self) -> logging.Logger:
        return self._facade.logger

    @property
    def settings(self) -> Settings:
        return self._facade.settings
