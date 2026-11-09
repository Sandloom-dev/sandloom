"""Kernel ASGI application factory: wires the core, modules and ops endpoints.

Ships in the ``sandloom`` distribution, so ANY application — including a
minimal ``sandloom new --no-batteries`` app that installs only ``sandloom`` —
builds its ASGI app with ``from sandloom.core.app_factory import create_app``.
The kernel names no business modules: which modules load is decided by
``SANDLOOM_MODULES`` (or the ``default_modules`` a composition root passes).
The standard root ``sandloom.main`` is a thin wrapper that loads every
installed ``sandloom.modules`` entry point.
"""

import logging
from collections.abc import Mapping, Sequence

from fastapi import FastAPI

import sandloom.core.admin.api.platform as _platform
import sandloom.core.app_factory.access_providers as _access_providers
import sandloom.core.app_factory.claim_authorities as _claim_authorities
import sandloom.core.app_factory.revocation_applier as _applier
import sandloom.core.app_factory.tenant_witness as _witness_consumes
import sandloom.core.consumers.runtime as _runtime
import sandloom.core.drivers.system_db as _system_db
import sandloom.core.facade._tenant_witness as _tenant_witness
import sandloom.core.guard_order as _guard_order
import sandloom.core.http.route_index as _route_index
import sandloom.core.http.route_runtime as _route_runtime
import sandloom.core.route_markers as _route_markers
import sandloom.core.route_policy as _route_policy
from sandloom.core import contract_cache_shadow
from sandloom.core import planner as _planner
from sandloom.core._internal.db_provider import get_db
from sandloom.core.app_factory.config import (
    DEVELOPMENT_DATABASE_RISK,
    application_enforced_isolation,
    configuration_problems,
    configured_database_urls,
    dbapi_config_problem,
    development_database_config_problem,
    enforce_configuration,
    nested_units_join_caller,
    outbox_relay_problem,
    read_only_sessions_config_problem,
    row_security_config_problem,
    sql_dialect_problem,
    sql_storage_problems,
    warn_application_enforced_isolation,
    warn_configuration_overrides,
    warn_nested_units_join_caller,
    warn_pair_mac_rpc,
    warn_weak_password_hashing,
)
from sandloom.core.app_factory.lifecycle import (
    POST_COMMIT_DRAIN_TIMEOUT_SECONDS,
    _BindActiveCore,
    _check_token_signing_role,
    _drain_post_commit_on_shutdown,
    _make_lifespan,
    _register_contract_compat_check,
    _register_health_checks,
    _register_infra_lifecycle,
    ensure_model_schemas,
    report_unreachable_database,
)
from sandloom.core.app_factory.ops import (
    ContractDiffRequest,
    _HealthCache,
    _install_ops_endpoints,
    _saga_diagnostics,
)
from sandloom.core.app_factory.pages import (
    _API_DOCS,
    _install_branded_docs,
    _install_favicon,
    _install_start_page,
)
from sandloom.core.claims import Authority, LegacyClaimsTokens
from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import ModuleLoadSummary
from sandloom.core.drivers.bootstrap import (
    build_infra_registry,
)
from sandloom.core.eventing.core_events import register_core_event_schemas
from sandloom.core.facade import CoreFacade
from sandloom.core.guarantee_windows import enforce_guarantee_windows
from sandloom.core.http.error_handlers import register_error_handlers
from sandloom.core.http.middlewares import register_middlewares
from sandloom.core.idempotency.durable import (
    install_durable_capture,
    validate_money_guards,
)
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.loader.manifest import AppManifest
from sandloom.core.loader.plugin_loader import (
    apply_module_defaults,
    load_modules_with_summary,
)
from sandloom.core.observability.logging import init_logging
from sandloom.core.observability.observability import init_sentry
from sandloom.core.orm import Base
from sandloom.core.rpc import (
    install_service_rpc_routes,
)
from sandloom.core.runtime import set_active_core
from sandloom.core.sagas import SystemChannel, install_sagas
from sandloom.core.schema.registry import InMemorySchemaRegistry

__all__ = [
    "Authority",
    "ConfigurationError",
    "LegacyClaimsTokens",
    "ContractDiffRequest",
    "DEVELOPMENT_DATABASE_RISK",
    "POST_COMMIT_DRAIN_TIMEOUT_SECONDS",
    "_API_DOCS",
    "_BindActiveCore",
    "_HealthCache",
    "_check_token_signing_role",
    "_drain_post_commit_on_shutdown",
    "_install_branded_docs",
    "_install_favicon",
    "_install_ops_endpoints",
    "_install_start_page",
    "_make_lifespan",
    "_register_contract_compat_check",
    "_register_health_checks",
    "_register_infra_lifecycle",
    "_saga_diagnostics",
    "application_enforced_isolation",
    "configuration_problems",
    "configured_database_urls",
    "create_app",
    "dbapi_config_problem",
    "development_database_config_problem",
    "enforce_configuration",
    "ensure_model_schemas",
    "nested_units_join_caller",
    "outbox_relay_problem",
    "read_only_sessions_config_problem",
    "report_unreachable_database",
    "row_security_config_problem",
    "sql_dialect_problem",
    "sql_storage_problems",
    "warn_application_enforced_isolation",
    "warn_configuration_overrides",
    "warn_nested_units_join_caller",
    "warn_pair_mac_rpc",
    "warn_weak_password_hashing",
]


def create_app(
    *,
    default_modules: Sequence[str] = (),
    default_optional_modules: Sequence[str] = (),
    title: str = "Sandloom Framework",
    claim_authorities: Mapping[str, str | Authority] | None = None,
    default_claim_authorities: Mapping[str, str | Authority] | None = None,
    legacy_tokens: LegacyClaimsTokens | None = None,
    access_providers: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build a fully wired Sandloom application (kernel + modules + ops API).

    ``default_modules`` apply only when the deployment configured no
    ``SANDLOOM_MODULES`` (the platform root passes identity/tenancy/billing); with
    the kernel defaults (empty) the module set comes from configuration
    alone. Every module is required unless named in
    ``default_optional_modules`` (defaulted lists only) or declared
    ``optional=True``.

    ``claim_authorities`` assigns each protected claim key to the module that
    is its authority, plus the token ``issuer`` (``{key: import path or
    entry-point name}``; ADR-0014): the mint keeps a protected claim only from
    its authority, and startup refuses an assignment the composition cannot
    honour (SANDLOOM-B035). ``default_claim_authorities`` applies when
    ``claim_authorities`` is not given and keeps only the entries whose module
    is part of the composition (loaded, or bound through its
    ``<m>.claims.v1``). With neither, no claim is assigned, and a composition
    that loads a token issuer (``issues_tokens=True``) refuses the boot.
    A value may be an ``Authority(name, previous=Authority(old, module=...))``
    for one rollout of a key's authority. Every verifier reads protected
    claims only through the trusted view of this map (per-key ``cpv``).

    ``legacy_tokens=LegacyClaimsTokens(until="<UTC time>")`` is the bounded
    compatibility mode of the initial ``cpv`` enablement (an accepted
    temporary risk, G-RISK-14): written for one upgrade, never by default,
    off by itself at ``until + ACCESS_TOKEN_EXPIRE_MINUTES +
    JWT_LEEWAY_SECONDS``.

    ``access_providers`` assigns an access-bearing contract to the module
    that provides it in-process (``{contract: import path or entry-point
    name}``; ADR-0014 decision 6): a contract a live adapter asks, or a key
    of this map, is registered only by the module its first segment names
    or by the one assigned here, checked by import path; anyone else refuses
    the boot (SANDLOOM-B034). Remotely, ``SERVICE_BINDINGS`` is the
    assignment. Every other contract keeps the first-registrant rule.
    """
    init_logging()  # ensure logging configured before modules import/log
    init_sentry()  # error reporting (no-op unless SENTRY_DSN set)

    # Checks the configuration layer cannot run itself (key material, RPC
    # keys / allowlist, the production profile): refuse to boot on any.
    enforce_configuration(settings)
    main_logger = logging.getLogger("sandloom.app")
    _warn_startup_configuration(main_logger)
    application = _new_application(title)

    core_facade = CoreFacade(app=application, get_db=get_db)
    application.state.core = core_facade

    # Kernel services
    schema_registry_service = InMemorySchemaRegistry()
    core_facade.register_schema_registry(schema_registry_service)

    infra = _bind_infrastructure(core_facade, schema_registry_service)
    _register_infra_lifecycle(core_facade, schema_registry_service, infra)

    # Register core baseline event schemas early (idempotent)
    register_core_event_schemas(core_facade)

    # Composition-root defaults: the kernel names no business modules, so when
    # the deployment configured neither SANDLOOM_MODULES nor MODULE_REQUIRED, the
    # caller's default set (if any) fills in. The same helper is the single
    # module-resolution rule (sandloom-migrate builds the composition root's app to
    # reuse it), so the CLI never migrates a different module set than it runs.
    apply_module_defaults(list(default_modules), list(default_optional_modules))

    # ADR-0014 decision 6: an access-bearing contract is registered only by
    # its provider (bound before the load, swept again after it).
    _access_providers.install_access_providers(
        core_facade, access_providers=access_providers, log=main_logger
    )
    # Load modules; a failed required module must abort startup loudly.
    module_summary: ModuleLoadSummary = load_modules_with_summary(core_facade)
    _access_providers.enforce_access_providers(core_facade, log=main_logger)
    application.state.module_summary = module_summary
    # Every model is declared now: each SQL database is one its driver serves,
    # and every partial index can be restricted there (B024, R015).
    storage_problems = sql_storage_problems(infra, Base.metadata)
    if storage_problems:
        raise ConfigurationError(INVALID_CONFIGURATION + "; ".join(storage_problems))
    warn_application_enforced_isolation(infra, Base.metadata, main_logger)
    warn_nested_units_join_caller(infra, main_logger)
    # The application manifest (spec §5.7), built from the load pass, no I/O.
    application.state.app_manifest = AppManifest.from_summary(module_summary)
    # ADR-0012 decision 5: the kernel applies durable token revocations in
    # every composition (its group is per deployment), and a token issuer
    # ends its refresh sessions on a block or declares it holds none.
    _applier.register_kernel_revocation_applier(core_facade, settings)
    _applier.enforce_issuer_obligation(
        module_summary.loaded,
        core_facade.event_consumers,
        application.state.app_manifest,
        log=main_logger,
    )
    # ADR-0014 decisions 1, 2, 7: the root's claim authorities, verified
    # against the composition, then applied by the mint check.
    _claim_authorities.install_claim_authorities(
        core_facade,
        module_summary.loaded,
        claim_authorities=claim_authorities,
        default_claim_authorities=default_claim_authorities,
        bindings=settings.service_bindings,
        log=main_logger,
        legacy_tokens=legacy_tokens,
        lifetime_seconds=settings.access_token_expire_minutes * 60,
        leeway_seconds=settings.jwt_leeway_seconds,
        booted_root=settings.sandloom_composition_root,
    )
    # ADR-0014 decision 13: a route policy stating Account.bound_tenant()
    # consumes the assigned tenant authority's witness (boot only, no I/O).
    if _witness_consumes.add_witness_consumes(
        core_facade,
        module_summary,
        core_facade.tenant_witness,
        _tenant_witness.bound_tenant_routes(application),
        log=main_logger,
    ):
        application.state.app_manifest = AppManifest.from_summary(module_summary)
    # Sagas (ADR-0008): check every registered definition against its
    # contracts, install the advance consumers and the timer sweep.
    saga_runtime = install_sagas(
        core_facade,
        system_channel=SystemChannel(
            open=_system_db.system_session,
            configured=_system_db.system_channel_configured,
        ),
    )
    # ADR-0005 guarantee windows, incl. the saga horizon (prod: refuse, else
    # warn).
    enforce_guarantee_windows(
        settings,
        log=main_logger,
        sagas=[r.definition for r in saga_runtime.registry],
    )
    _check_token_signing_role(core_facade, module_summary, main_logger)
    install_service_rpc_routes(application, core_facade)

    _register_contract_compat_check(core_facade)

    consumer_runtime = _runtime.ConsumerRuntime(
        core_facade,
        startup_timeout=settings.consumer_startup_timeout_seconds,
        retry_delay=settings.consumer_retry_base_seconds,
        retry_max=settings.consumer_retry_max_seconds,
        shutdown_timeout=settings.shutdown_task_timeout_seconds,
    )
    application.state.consumer_runtime = consumer_runtime

    _register_health_checks(core_facade, infra)
    if settings.environment.value == "dev":

        async def report_unreachable_dev_database() -> None:
            await report_unreachable_database(core_facade)

        core_facade.add_startup_task(report_unreachable_dev_database, phase="post")

    application.router.lifespan_context = _make_lifespan(
        application, core_facade, module_summary, consumer_runtime
    )
    # Outermost: every request runs with this app's core bound to its context,
    # so a second app built in the same process (which moves the process
    # default) never receives this app's events or units.
    application.add_middleware(_BindActiveCore, core=core_facade)

    _install_ops_endpoints(application, core_facade)

    _platform.install(application, core_facade)
    _install_start_page(application)
    _install_favicon(application)
    _validate_and_index_routes(application)
    return application


def _warn_startup_configuration(main_logger: logging.Logger) -> None:
    if "environment" not in settings.model_fields_set:
        main_logger.warning(
            "ENVIRONMENT is not set; defaulting to 'dev' (docs exposed, rate "
            "limiting off, volatile event bus allowed). Set ENVIRONMENT=prod "
            "explicitly for any real deployment."
        )
    if not settings.internal_api_token and not (
        settings.internal_endpoints_open_in_dev and not settings.is_prod
    ):
        main_logger.warning(
            "INTERNAL_API_TOKEN is unset: /metrics, /diagnostics, /contracts and "
            "/health details are disabled (404). Set the token, or "
            "INTERNAL_ENDPOINTS_OPEN_IN_DEV=true for local development."
        )
    warn_configuration_overrides(settings, main_logger)
    warn_pair_mac_rpc(settings, main_logger)
    warn_weak_password_hashing(settings, main_logger)


def _new_application(title: str) -> FastAPI:
    # Interactive docs are served outside production by default; in production
    # they are disabled unless DOCS_ENABLED is explicitly set.
    # Outside production the kernel serves /docs and /redoc itself, in the
    # Sandloom colours; in production they are the app's own: FastAPI's.
    branded = settings.docs_exposed and not settings.is_prod
    docs_kwargs: dict[str, str | None] = (
        {"docs_url": None, "redoc_url": None}
        if branded
        else {}
        if settings.docs_exposed
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    application = FastAPI(title=title, version="1.0.0", **docs_kwargs)  # type: ignore[arg-type]
    if branded:
        _install_branded_docs(application)
    # Order matters: Starlette makes the LAST-added middleware the OUTERMOST.
    # register_middlewares places the Mask5xx safety net just INSIDE CORS (so
    # masked 500s still carry CORS headers) and outside every other framework
    # middleware (idempotency capture, body limit, process-time), so it also
    # masks exceptions raised inside them — not just those from the router.
    # ADR-0003 contract-read cache SHADOW (off by default): attribute watched
    # reads to the HTTP request that made them. Installed only while the
    # shadow is on (innermost, so the matched route is visible) — zero cost
    # otherwise.
    if contract_cache_shadow.current() is not None:
        application.add_middleware(contract_cache_shadow.ShadowRequestMiddleware)
    register_middlewares(application)
    register_error_handlers(application)
    return application


def _bind_infrastructure(
    core_facade: CoreFacade, schema_registry_service: InMemorySchemaRegistry
) -> InfraRegistry:
    """Build and bind the infra registry, the default event bus and the core."""
    # Infrastructure plugin layer: build the registry (built-in drivers +
    # entry-point plugins + merged config) and bind it. All infrastructure —
    # databases, caches, object stores, queues, the event bus — is resolved
    # through it by logical name, so modules stay infrastructure-agnostic and
    # multiple instances (e.g. several databases) coexist via configuration.
    infra = build_infra_registry(schema_registry_service)
    core_facade.set_infra(infra)

    # The event bus is the default eventbus instance; bind it everywhere the
    # lazy module-level accessor is used (outbox dispatch, relay).
    eventbus_provider = core_facade.eventbus_provider("default")
    # In production the default bus MUST be durable. EVENT_BUS_BACKEND=auto
    # already selects Redis Streams in prod, but an explicit override to the
    # in-memory or pub/sub bus would deliver domain events (billing credits,
    # provisioning, …) at-most-once and silently drop them with no error. Fail
    # fast at startup instead. Dev/test intentionally run the volatile bus.
    if settings.is_prod and "durable" not in eventbus_provider.features():
        raise RuntimeError(
            "Production event bus is volatile "
            f"(driver={eventbus_provider.driver!r}, "
            f"features={sorted(eventbus_provider.features())}). Domain events "
            "would be lost. Set EVENT_BUS_BACKEND=auto (or streams) to use the "
            "durable Redis Streams transport."
        )
    bus = eventbus_provider.bus()
    core_facade.set_event_bus(bus)
    # Bind the app-owned runtime (the core with its registry and bus, in one
    # call) so non-request callers (post-commit dispatch, relay helpers,
    # Celery tasks via db_session, cache() in a script) resolve THIS
    # application's instances.
    set_active_core(core_facade)

    core_facade.ensure_tracer()
    return infra


def _validate_and_index_routes(application: FastAPI) -> None:
    # Model E (ADR-0009): every API route is authenticated xor public(reason=).
    _route_policy.validate_route_policy(application.routes)
    # spec §6.1: a write route's write store is unambiguous (SANDLOOM-B009/B013).
    _route_policy.validate_write_stores(application.routes)
    # spec §6.1: the write store provides what the route defaults need
    # (SANDLOOM-B010) and no non-transactional store is injected (SANDLOOM-B011).
    _route_runtime.validate_store_capabilities(
        application.routes, getattr(application.state.core, "infra", None)
    )
    # A SnapshotReads route reads one snapshot: the default database must
    # have snapshot_reads, never a silent read without one (SANDLOOM-B027).
    snapshot_problems = _planner.snapshot_reads_problems(
        application.routes, getattr(application.state.core, "infra", None)
    )
    if snapshot_problems:
        raise _planner.SnapshotReadsUnavailable("; ".join(snapshot_problems))
    # An idempotency guard must be solved AFTER the route's authentication
    # (A-03): refuse a route that would reserve / replay for a caller it has
    # not authenticated yet.
    _guard_order.validate_guard_order(application.routes)
    _guard_order.validate_gate_order(application.routes)
    # ADR-0005 / ADR-0009: a money route may not rely on a replay-only (cache)
    # or fail-open idempotency guard — its record is written after COMMIT, so
    # an ambiguous COMMIT re-runs the effect (SANDLOOM-B019). Durable (or in-flight
    # + endpoint-owned) only.
    validate_money_guards(application.routes)
    # Those declarations are read off the routes, never executed: stop FastAPI
    # from solving the no-op marker dependencies on every request (recorded on
    # the route; route.dependencies, OpenAPI and every reader unchanged).
    _route_markers.prune_marker_dependencies(application.routes)
    # The per-route runtime of model E: units, write admission, the lazy
    # request unit, the commit-time re-check, no-store (spec §4); it also
    # hands a durable guard its response before the request's UoW commits.
    _route_runtime.validate_capability_replay(application.routes)
    _route_runtime.install_route_runtime(application.routes, application.state.core)
    # Routes outside the runtime's reach keep the durable capture.
    install_durable_capture(application.routes)
    # Every module's routes are registered now: dispatch through the
    # first-segment index instead of Starlette's linear scan (identical
    # matching semantics; re-indexes itself if routes are added later).
    _route_index.install_route_index(application.router)


#: Shutdown budget for draining the background post-commit dispatcher.
