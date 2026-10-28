"""The state every :class:`CoreFacade` carries, set up by its constructor.

The base of the facade's mixins (:mod:`._services`, :mod:`._claims`,
:mod:`._registries`): the registries, lifecycle collections, contract and
claims bookkeeping and hot-path caches they share.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

if TYPE_CHECKING:  # pragma: no cover - typing only
    # SQLAlchemy is the relational paradigm's session type; the facade only
    # names it in annotations, so the import is typing-only — the paradigm-
    # agnostic kernel carries no runtime dependency on a concrete backend.
    from sqlalchemy.ext.asyncio import AsyncSession


from sandloom.core.config.settings import Settings, settings
from sandloom.core.contracts import (
    SchemaRegistryContract,
)
from sandloom.core.descriptors import HealthCheck
from sandloom.core.module_api._service_registry import (
    ServiceRegistry,
)
from sandloom.core.projections.registry import QueryProjectionRegistry

StartupTask = Callable[[], Awaitable[None]] | Callable[[], None]
ShutdownTask = Callable[[], Awaitable[None]] | Callable[[], None]


class _FacadeState:
    """CoreFacade methods: see :mod:`sandloom.core.facade._state`."""

    loaded_modules: list[str]

    def __init__(
        self,
        app: FastAPI,
        settings_obj: Settings | None = None,
        service_registry: ServiceRegistry | None = None,
        # get_db may be coroutine returning AsyncSession OR async generator dependency
        get_db: (
            Callable[[], Awaitable[AsyncSession]]
            | Callable[[], AsyncGenerator[AsyncSession, None]]
            | None
        ) = None,
    ) -> None:
        """Create facade.

        Only expose methods that are safe / stable for modules. Keep internal infra
        details (like concrete cache or tracing impl choices) hidden behind this API.
        """
        # Core infra references
        self.app = app
        self.settings = settings_obj or settings
        self.service_registry = service_registry or ServiceRegistry()
        # Remote-service-proxy factory for cross-process service-registry
        # contracts. Injected by sandloom.core.rpc.install_service_rpc_routes at
        # composition (signature: (name, binding, *, default_timeout_seconds,
        # settings) -> proxy), so the facade builds a remote
        # fallback WITHOUT importing the concrete RPC mechanism. None until wired
        # (a bare facade / a deployment with no RPC): get_service then degrades
        # to ServiceUnavailableInDeployment for any non-local contract.
        self._remote_service_resolver: Callable[..., Any] | None = None
        self.get_db = get_db  # dependency provider (async) injected from db module
        self.logger = logging.getLogger("sandloom.core").getChild("facade")

        # Infra singletons / lazies
        self._tracer_initialized = False

        # Metrics placeholder (Phase A skeleton, real metrics Phase G)
        self._metrics: dict[str, Any] = {}

        # Lifecycle collections
        # Criticality is tracked per REGISTRATION (parallel to _startup_tasks),
        # not as a set keyed by task identity — so the same callable registered
        # twice with different critical flags keeps each flag, and tasks with a
        # custom __eq__/__hash__ (e.g. functools.partial) can't collide.
        self._startup_tasks: list[StartupTask] = []
        self._startup_critical: list[bool] = []
        # Startup phase per registration ("infra" | "modules" | "post").
        self._startup_phase: list[str] = []
        self._shutdown_tasks: list[ShutdownTask] = []
        self._health_checks: dict[str, HealthCheck] = {}

        # Token claims contributors (populated by modules; invoked at mint
        # time). Each is ``(fn, critical, owner)``: a critical contributor's
        # failure aborts minting instead of silently issuing a reduced-claims
        # token; ``owner`` is the short name of the module that registered it
        # (provenance stamped into ``claims_sources``).
        self._claims_contributors: list[tuple[Any, bool, str | None]] = []
        # Contract ownership: service name -> registering module (full name),
        # and explicit per-contract database overrides.
        self._service_owners: dict[str, str] = {}
        self._service_databases: dict[str, str] = {}
        # claims owner (short name) -> full module name that registered it, so
        # a contributor runs on ITS module's database even when its
        # ``<owner>.claims.v1`` contract could not be registered.
        self._claims_owner_modules: dict[str, str] = {}
        self._known_module_packages: set[str] = set(
            getattr(self.settings, "sandloom_modules", ()) or ()
        )
        self._module_database_cache: dict[str, str] = {}
        self._module_descriptors: list[Any] = []
        self._module_descriptor_index: dict[str, Any] = {}
        # get_service hot path: contract name -> (registered impl, database,
        # its ContractProvider) and -> (resolver, binding, token, timeout,
        # remote proxy). Revalidated on every call, so a re-registered impl, a
        # changed database or binding builds a fresh one.
        self._bound_contracts: dict[str, tuple[Any, str, Any]] = {}
        self._remote_proxies: dict[str, tuple[Any, Any, Any, Any]] = {}
        # Real counters behind increment_metric (atomic under this lock).
        self._metrics_lock = threading.Lock()
        # Name of the module whose registration is being committed (set by
        # module_registration) — attributes contributors to their owner.
        self._registering_module: str | None = None

        # Access facts (populated by modules). Maps a fact name to the
        # AccessFact descriptor that binds its claim/projection/live resolution
        # paths; read by the authorization subsystem (sandloom.shared.authz.facts).
        self._access_facts: dict[str, Any] = {}
        # Facts owned by modules deployed elsewhere, described over their
        # ``<owner>.facts.v1`` contract (TTL-cached by sandloom.shared.authz.facts).
        self._remote_access_fact_cache: dict[Any, Any] = {}

        # Event bus instance owned by this application (bound in create_app)
        self._event_bus: Any | None = None

        # Module tracking (populated when modules register themselves)
        self.loaded_modules = []
        self.module_descriptors = []  # readiness / metadata

        # Infrastructure registry (wired in main). Source of truth for all
        # pluggable, config-selected infrastructure instances.
        self._infra: Any | None = None

        # (Phase J placeholder: schema registry will be integrated later)
        self._schema_registry: SchemaRegistryContract | None = None

        # Cross-module query/projection registry. Pure in-memory metadata, always
        # available (no I/O), populated by read modules at registration and read
        # by the consumer runtime + rebuild CLI.
        self._projection_registry = QueryProjectionRegistry()

        # Generic durable event consumers (the "react to an event with a side
        # effect in your own UoW" primitive). Populated by modules at
        # registration; subscribed to the bus at startup.
        self._event_consumers: list[Any] = []
        # Saga definitions (ADR-0008): (definition, registering module). Bound
        # and checked by sandloom.core.sagas.install_sagas after every module loaded;
        # the runtime / sweep it builds are stored here.
        self._sagas: list[tuple[Any, str]] = []
        self.saga_runtime: Any | None = None
        self.saga_sweeper: Any | None = None

    @property
    def module_descriptors(self) -> list[Any]:
        """Loaded module metadata; replace the list to refresh resolved targets."""
        return list(self._module_descriptors)

    @module_descriptors.setter
    def module_descriptors(self, descriptors: list[Any]) -> None:
        self._module_descriptors = list(descriptors)
        self._module_descriptor_index = {str(d.name): d for d in descriptors}
        self._module_database_cache.clear()
        self._known_module_packages.update(self._module_descriptor_index)
