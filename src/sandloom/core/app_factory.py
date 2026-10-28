"""Kernel ASGI application factory: wires the core, modules and ops endpoints.

Ships in the ``sandloom`` distribution, so ANY application — including a
minimal ``sandloom new --no-batteries`` app that installs only ``sandloom`` —
builds its ASGI app with ``from sandloom.core.app_factory import create_app``.
The kernel names no business modules: which modules load is decided by
``SANDLOOM_MODULES`` (or the ``default_modules`` a composition root passes).
The standard root ``sandloom.main`` is a thin wrapper that loads every
installed ``sandloom.modules`` entry point.
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, cast

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel

import sandloom.core.admin.api.platform as _platform
import sandloom.core.auth.revocation as _revocation
import sandloom.core.auth.tokens as _tokens
import sandloom.core.consumers.runtime as _runtime
import sandloom.core.contracts.validators as _validators
import sandloom.core.drivers.system_db as _system_db
import sandloom.core.guard_order as _guard_order
import sandloom.core.http.route_index as _route_index
import sandloom.core.http.route_runtime as _route_runtime
import sandloom.core.observability.tracing as _tracing
import sandloom.core.route_markers as _route_markers
import sandloom.core.route_policy as _route_policy
import sandloom.core.tasks.pool_metrics as _pool_metrics
import sandloom.core.unit_of_work.dispatch as _dispatch
from sandloom.core import contract_cache_shadow, rpc_binding, rpc_identity, rpc_pair_mac
from sandloom.core import planner as _planner
from sandloom.core._internal.db_provider import get_db
from sandloom.core.auth.tokens import jwt_key_material_problem
from sandloom.core.config import tiers
from sandloom.core.config.common import INVALID_CONFIGURATION, strict_profile_active
from sandloom.core.config.password_params import meets_owasp_minimum
from sandloom.core.config.settings import load_settings, settings
from sandloom.core.contracts import SchemaEntry
from sandloom.core.contracts.validators import classify_schema_change
from sandloom.core.descriptors import HealthResult, ModuleLoadSummary
from sandloom.core.descriptors.module_names import resolve_module_names
from sandloom.core.drivers.bootstrap import (
    build_infra_registry,
    close_default_cache,
    close_default_database,
)
from sandloom.core.eventing.bus_metrics import get_event_bus_metrics
from sandloom.core.eventing.core_events import register_core_event_schemas
from sandloom.core.eventing.event_bus import set_active_event_bus
from sandloom.core.facade import CoreFacade
from sandloom.core.guarantee_windows import enforce_guarantee_windows
from sandloom.core.http.error_handlers import register_error_handlers
from sandloom.core.http.middlewares import register_middlewares
from sandloom.core.idempotency.durable import (
    install_durable_capture,
    validate_money_guards,
)
from sandloom.core.infra.active import set_active_infra
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.internal_auth import (
    has_internal_access,
    require_internal_access,
    require_metrics_access,
)
from sandloom.core.lifecycle import (
    aggregate_health,
    overall_status,
    run_shutdown,
    run_startup,
)
from sandloom.core.loader.manifest import AppManifest
from sandloom.core.loader.plugin_loader import (
    apply_module_defaults,
    load_modules_with_summary,
)
from sandloom.core.observability import metrics
from sandloom.core.observability.logging import init_logging
from sandloom.core.observability.metrics import render_metrics
from sandloom.core.observability.observability import init_sentry
from sandloom.core.production_profile import config_problems
from sandloom.core.production_profile import (
    enforce_runtime as enforce_production_profile,
)
from sandloom.core.rpc import (
    check_remote_contract_compat,
    check_remote_service_bindings,
    install_service_rpc_routes,
)
from sandloom.core.runtime import set_active_core
from sandloom.core.sagas import SystemChannel, install_sagas
from sandloom.core.schema.registry import InMemorySchemaRegistry


async def _core_health() -> HealthResult:
    return HealthResult(status="ok")


async def _capability_health(
    core_facade: CoreFacade,
    capability: str,
    *,
    name: str = "default",
) -> HealthResult:
    """Probe the configured provider selected by the infra registry."""
    try:
        provider = core_facade.capability(capability, name)
        return cast(HealthResult, await provider.health())
    except Exception as exc:  # noqa: BLE001 - any provider failure is a failed probe
        return HealthResult(status="fail", details={"error": str(exc)})


class ConfigurationError(SystemExit):
    """The configuration fails a check that runs at boot.

    A ``SystemExit`` like a ``Settings`` load failure: the entry point
    (ASGI import, worker, ``sandloom-migrate``, the ``sandloom`` CLI) stops with the
    message and exit status 1 before serving or touching anything.
    """


def configuration_problems(cfg: Any, *, include_profile: bool = True) -> list[str]:
    """Every startup configuration problem of ``cfg`` (empty = usable).

    ``Settings`` validates what the configuration layer can judge alone;
    these checks need kernel code above it and therefore run when a process
    boots (:func:`enforce_configuration`: ``create_app`` — the API, the worker
    and the ops CLIs boot through it — and the ``sandloom-migrate`` / ``sandloom``
    command lines):

    * the asymmetric JWT key material
      (:func:`sandloom.core.auth.tokens.jwt_key_material_problem`);
    * the internal-RPC keys, bindings and allowlist
      (``rpc_binding`` / ``rpc_pair_mac`` / ``rpc_identity``
      ``configuration_problem``);
    * ``PRODUCTION_PROFILE=multi_tenant_strict`` in prod
      (:func:`sandloom.core.production_profile.config_problems`), unless
      ``include_profile=False`` (``sandloom doctor`` reports the profile itself).
    """
    problems: list[str] = []
    for check in (
        jwt_key_material_problem,
        rpc_binding.configuration_problem,
        rpc_pair_mac.configuration_problem,
        rpc_identity.configuration_problem,
    ):
        problem = check(cfg)
        if problem:
            problems.append(problem)
    if include_profile and strict_profile_active(cfg):
        errors, _warnings = config_problems(cfg)
        if errors:
            problems.append(
                "PRODUCTION_PROFILE=multi_tenant_strict is not met: "
                + "; ".join(errors)
            )
    return problems


def enforce_configuration(cfg: Any = None) -> None:
    """Refuse to boot on any :func:`configuration_problems` finding.

    ``cfg`` defaults to the process settings, which this forces (settings
    are lazy, spec §2.4: an invalid environment fails here, at startup).
    """
    problems = configuration_problems(load_settings() if cfg is None else cfg)
    if problems:
        raise ConfigurationError(INVALID_CONFIGURATION + "; ".join(problems))


def create_app(
    *,
    default_modules: Sequence[str] = (),
    default_optional_modules: Sequence[str] = (),
    title: str = "Sandloom Framework",
) -> FastAPI:
    """Build a fully wired Sandloom application (kernel + modules + ops API).

    ``default_modules`` apply only when the deployment configured no
    ``SANDLOOM_MODULES`` (the platform root passes identity/tenancy/billing); with
    the kernel defaults (empty) the module set comes from configuration
    alone. Every module is required unless named in
    ``default_optional_modules`` (defaulted lists only) or declared
    ``optional=True``.
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

    # Load modules; a failed required module must abort startup loudly.
    module_summary: ModuleLoadSummary = load_modules_with_summary(core_facade)
    application.state.module_summary = module_summary
    # The application manifest (spec §5.7), built from the load pass, no I/O.
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

    application.router.lifespan_context = _make_lifespan(
        application, core_facade, module_summary, consumer_runtime
    )

    _install_ops_endpoints(application, core_facade)

    _platform.install(application, core_facade)
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
    docs_kwargs: dict[str, str | None] = (
        {}
        if settings.docs_exposed
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    application = FastAPI(title=title, version="1.0.0", **docs_kwargs)  # type: ignore[arg-type]
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
    # Bind the process-wide accessor so non-request callers (Celery tasks,
    # scripts, service helpers) can resolve default instances by logical name.
    set_active_infra(infra)

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
    set_active_event_bus(bus)
    # Bind the app-owned runtime so non-request callers (post-commit dispatch,
    # relay helpers, Celery tasks via db_session) resolve THIS application's bus
    # and UoW factories, not a process-global accessor (multi-app safe).
    set_active_core(core_facade)

    core_facade.ensure_tracer()
    return infra


def _register_infra_lifecycle(
    core_facade: CoreFacade,
    schema_registry_service: InMemorySchemaRegistry,
    infra: InfraRegistry,
) -> None:
    """Infra startup tasks, then infra teardown (before modules load)."""

    async def infra_load_schema_registry() -> None:
        """Startup task to load schemas from persistence layer.

        Strict and critical in production: a broken SCHEMA_REGISTRY_PATH file
        aborts startup instead of serving without its persisted schemas
        (a missing file is a first boot, never an error).
        """
        schema_registry_service.configure_persistence(settings.schema_registry_path)
        schema_registry_service.load_from_disk(strict=settings.is_prod)

    core_facade.add_startup_task(
        infra_load_schema_registry, critical=settings.is_prod, phase="infra"
    )

    async def infra_warm_instances() -> None:
        """Eagerly open configured infra instances before serving traffic.

        Strict in production: a configured database/cache/object store the
        process cannot reach aborts startup (critical task) instead of letting
        the app serve traffic that fails on first use. In dev it stays
        best-effort so a missing optional backend does not block local work.
        """
        await infra.aopen_all(strict=settings.is_prod)

    core_facade.add_startup_task(
        infra_warm_instances, critical=settings.is_prod, phase="infra"
    )

    if strict_profile_active(settings):

        async def production_profile_check() -> None:
            """PRODUCTION_PROFILE=multi_tenant_strict: RLS covers every tenant
            table (the configuration half is enforced by Settings)."""
            await enforce_production_profile(core_facade)

        core_facade.add_startup_task(
            production_profile_check, critical=True, phase="infra"
        )

    # Infra teardown — registered BEFORE modules load so run_shutdown
    # (which executes in reverse order) tears infra down last.
    async def shutdown_tracer() -> None:
        await asyncio.to_thread(_tracing.shutdown_tracing)

    async def dispose_engine() -> None:
        await close_default_database()

    async def shutdown_cache() -> None:
        await close_default_cache()

    async def close_infra() -> None:
        # Close registry-owned infrastructure: the event bus and any named
        # (non-default) database/cache/object-store instances. The shared
        # default engine/cache are disposed by their own tasks above.
        await infra.aclose_all()

    # Flush after module/infra teardown so their final spans are included;
    # registration also covers cleanup after a failed startup.
    core_facade.add_shutdown_task(shutdown_tracer)
    core_facade.add_shutdown_task(dispose_engine)
    core_facade.add_shutdown_task(shutdown_cache)
    core_facade.add_shutdown_task(close_infra)


def _register_contract_compat_check(core_facade: CoreFacade) -> None:
    async def contract_compat_check() -> None:
        """Rolling-upgrade guard: compare remote providers with our snapshots.

        Logs an incompatible provider (``CONTRACT_COMPAT_CHECK=ready``; /ready
        then keeps failing until it is fixed) or aborts startup (``strict``).
        """
        await check_remote_contract_compat(core_facade)

    if settings.contract_compat_check != "off" and settings.service_bindings:
        core_facade.add_startup_task(
            contract_compat_check,
            critical=settings.contract_compat_check == "strict",
            phase="post",
        )


def _register_health_checks(core_facade: CoreFacade, infra: InfraRegistry) -> None:
    async def db_health() -> HealthResult:
        return await _capability_health(core_facade, "database")

    async def cache_health() -> HealthResult:
        return await _capability_health(core_facade, "cache")

    async def storage_health() -> HealthResult:
        return await _capability_health(core_facade, "objectstore")

    async def eventbus_health() -> HealthResult:
        return await _capability_health(core_facade, "eventbus")

    core_facade.register_health_check("core", _core_health)
    core_facade.register_health_check("db", db_health)
    core_facade.register_health_check("cache", cache_health)
    core_facade.register_health_check("storage", storage_health)
    core_facade.register_health_check("eventbus", eventbus_health)

    # /health is dependency diagnostics; /health/simple is liveness.
    # Remote peers feed /ready only for required bindings.
    async def infra_instances_health() -> HealthResult:
        """Probe NAMED (non-default) infra instances.

        Default db/cache/storage are covered by the dedicated checks above;
        this surfaces additional databases/caches/object-stores declared via
        INFRA so /ready reflects them too.
        """
        results = await infra.health_all()
        failures = {
            key: res.details
            for key, res in results.items()
            if not key.endswith(":default") and res.status != "ok"
        }
        if failures:
            return HealthResult(status="fail", details={"instances": failures})
        return HealthResult(status="ok")

    core_facade.register_health_check("infra", infra_instances_health)


def _make_lifespan(
    application: FastAPI,
    core_facade: CoreFacade,
    module_summary: ModuleLoadSummary,
    consumer_runtime: Any,
) -> Callable[[FastAPI], Any]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
        try:
            # Validate the role before any startup task can open a DB/cache.
            # The registered critical task also protects direct lifecycle use.
            problem = _tokens.token_signing_problem(
                issuer_loaded=any(
                    getattr(d, "issues_tokens", False) for d in module_summary.loaded
                )
            )
            if problem is not None:
                raise RuntimeError(problem)
            relay_problem = outbox_relay_problem(settings)
            if relay_problem is not None:
                raise RuntimeError(relay_problem)
            await run_startup(core_facade)
            await consumer_runtime.start()
        except BaseException:
            # Startup aborted (e.g. a critical task failed). The lifespan's
            # try/finally below never armed, so tear down whatever create_app
            # already opened (DB engine, cache, named infra instances) before
            # re-raising — otherwise a failed start leaks those connections.
            await consumer_runtime.stop()
            await run_shutdown(core_facade)
            raise
        relay_task = relay_stop = None
        relay_mode = settings.outbox_relay_enabled
        if relay_mode == "on" or (relay_mode == "auto" and settings.is_prod):
            from sandloom.core.tasks.relay import start_relay_background  # noqa: PLC0415 - cold: the relay registers its metrics on import; only a process that runs it may expose them

            relay_task, relay_stop = start_relay_background(core_facade)
        application.state.relay_task = relay_task

        pool_task, pool_stop = _pool_metrics.start_pool_sampler(core_facade)
        try:
            yield
        finally:
            # Requests have stopped: let the background post-commit dispatcher
            # publish / mark what the last requests committed (bounded — what
            # is left stays in the outbox for the relay of the next process).
            deadline = (
                asyncio.get_running_loop().time() + settings.shutdown_budget_seconds
            )
            pool_stop.set()
            await asyncio.gather(pool_task, return_exceptions=True)
            try:
                async with asyncio.timeout_at(deadline):
                    await _drain_post_commit_on_shutdown()
                    if relay_task is not None and relay_stop is not None:
                        relay_stop.set()
                        try:
                            await asyncio.wait_for(relay_task, timeout=10)
                        except TimeoutError:
                            relay_task.cancel()
                            await asyncio.gather(relay_task, return_exceptions=True)
                    await consumer_runtime.stop()
                    await run_shutdown(core_facade, deadline=deadline)
            except TimeoutError:
                _shutdown_logger.error(
                    "Shutdown exceeded %.1fs budget; unfinished durable work "
                    "remains recoverable. The process supervisor grace period "
                    "must exceed this budget.",
                    settings.shutdown_budget_seconds,
                )

    return lifespan


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
POST_COMMIT_DRAIN_TIMEOUT_SECONDS = 5.0
_shutdown_logger = logging.getLogger("sandloom.app")


async def _drain_post_commit_on_shutdown() -> None:
    try:
        await _dispatch.drain_post_commit(timeout=POST_COMMIT_DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        _shutdown_logger.warning(
            "post-commit dispatch not drained within %.1fs; the outbox relay "
            "delivers the rest",
            POST_COMMIT_DRAIN_TIMEOUT_SECONDS,
        )
    except Exception:  # shutdown must proceed
        _shutdown_logger.warning("post-commit drain failed on shutdown", exc_info=True)


def warn_pair_mac_rpc(cfg: Any, log: logging.Logger) -> list[str]:
    """Startup warnings of pair-MAC internal RPC (ADR-0004); returns them."""

    if not (cfg.service_bindings or cfg.rpc_dh_private_key):
        return []  # this process neither calls nor can serve internal RPC
    out: list[str] = []
    stale = rpc_pair_mac.stale_extra_kids(cfg)
    if stale:
        out.append(
            f"RPC_DH_EXTRA_PRIVATE_KEYS {stale} are no longer published in "
            "RPC_SERVICE_DH_KEYS: a retired provider key still derives every "
            "caller's pair key towards this service. Drop it (the rotation "
            "overlap is bounded: docs/adr/0004, 'Key rotation')."
        )
    if not cfg.is_prod and cfg.rpc_replay_guard != "local":
        out.append(
            "RPC nonce guard: the shared (security cache) layer FAILS OPEN "
            "outside prod — a cache outage accepts a replay on another worker "
            "or replica (prod fails closed)."
        )
    if not rpc_pair_mac.commands_require_signature(cfg):
        out.append(
            "RPC_COMMANDS_REQUIRE_SIGNATURE is off: a leaked provider X25519 key "
            "can forge COMMAND calls from any caller to that provider (on in "
            "prod by default)."
        )
    for line in out:
        log.warning(line)
    return out


def warn_configuration_overrides(cfg: Any, log: logging.Logger) -> list[tiers.Override]:
    """Log the profile overrides of ``cfg`` once at startup; returns them.

    A semantic setting moved off its profile value, or an accepted risk
    (:mod:`sandloom.core.config.tiers`): an operator-facing log line plus
    ``sandloom_config_override_active{setting,kind}``.
    """
    found = tiers.log_overrides(cfg, log)
    for item in found:
        metrics.config_override_active.labels(item.setting, item.kind).set(1)
        if item.kind == "accepted_risk":
            metrics.accepted_risk_active.labels(str(item.value)).set(1)
    return found


def outbox_relay_problem(cfg: Any) -> str | None:
    """Why this prod app process must not start without an outbox relay.

    ``OUTBOX_RELAY_ENABLED=off`` leaves committed events that the post-commit
    dispatch failed to publish in the outbox for good: delivery degrades from
    at-least-once to a single attempt. Refused in prod unless the deployment
    accepts the risk ``relay_disabled``. ``delegated`` (set by the gunicorn
    config in every worker but the relay leader) is fine inside a gunicorn
    worker (``SANDLOOM_WORKER_COUNT`` set by its ``post_fork``).
    """
    if not cfg.is_prod:
        return None
    mode = cfg.outbox_relay_enabled
    if mode == "delegated" and os.environ.get("SANDLOOM_WORKER_COUNT"):
        return None
    if mode not in ("off", "delegated") or cfg.accepts_risk("relay_disabled"):
        return None
    return (
        f"OUTBOX_RELAY_ENABLED={mode} in a production app process: no process "
        "retries the outbox, so an event whose post-commit dispatch failed is "
        "never delivered. Fix: leave it auto (the gunicorn config keeps exactly "
        "one relay per process group), or accept the risk explicitly with "
        'SANDLOOM_ACCEPTED_RISKS=["relay_disabled"] when no process should relay'
    )


def warn_weak_password_hashing(cfg: Any, log: logging.Logger) -> bool:
    """Log argon2id parameters below the OWASP minimum on every start.

    In prod they only get this far with the accepted risk ``weak_password_hash`` (the
    settings validator refuses them otherwise), so the line is loud; outside
    prod weak parameters are a normal fast-test setting and get a warning.
    """
    params = cfg.password_hash_params
    if meets_owasp_minimum(params):
        return False
    if cfg.is_prod:
        log.warning(
            "SECURITY: password hashing runs with argon2id %s, BELOW the OWASP "
            "minimum (m=19456 KiB, t=2, p=1), because the risk weak_password_hash "
            "is accepted. Stolen password hashes are cheaper to crack; switch "
            "PASSWORD_HASH_PROFILE to owasp_min or strong.",
            params.describe(),
        )
    else:
        log.warning(
            "password hashing uses argon2id %s, below the OWASP minimum "
            "(allowed outside prod; refused in prod)",
            params.describe(),
        )
    return True


def _check_token_signing_role(
    core_facade: CoreFacade, summary: ModuleLoadSummary, log: logging.Logger
) -> None:
    """Refuse to serve when the JWT key material does not fit this process.

    See :func:`sandloom.core.auth.tokens.token_signing_problem`: an asymmetric issuer
    needs the private key, a verify-only service must hold neither the private
    key nor a symmetric secret, and a split production deployment must not
    share an HS secret. A CRITICAL startup task, so the lifespan aborts.
    """

    issuer_loaded = any(
        getattr(descriptor, "issues_tokens", False) for descriptor in summary.loaded
    )
    problem = _tokens.token_signing_problem(issuer_loaded=issuer_loaded)
    if problem is None:
        return
    log.error("token signing: %s", problem)

    async def _refuse_token_signing_role() -> None:
        raise RuntimeError(problem)

    core_facade.add_startup_task(
        _refuse_token_signing_role, critical=True, phase="infra"
    )


def _required_modules(core_facade: CoreFacade) -> list[str]:
    """Modules this process cannot serve without.

    Settings-declared required modules count only when they were actually
    requested for this process (SANDLOOM_MODULES subsetting must keep working),
    minus any loaded descriptor that declares itself ``optional=True``.
    """
    # Entries name a module by import path or entry-point name: compare the
    # import paths (what descriptors carry).
    requested = set(
        resolve_module_names(list(getattr(settings, "sandloom_modules", []) or []))
    )
    optional = {
        d.name
        for d in getattr(core_facade, "module_descriptors", [])
        if getattr(d, "optional", False)
    }
    required = resolve_module_names(
        list(getattr(settings, "module_required", []) or [])
    )
    return [
        name
        for name in dict.fromkeys(required)
        if name in requested and name not in optional
    ]


class _HealthCache:
    """Short-TTL, single-flight cache of the aggregated health probes.

    ``/health`` and ``/ready`` are public: without a cache every anonymous hit
    fans out to the database, cache, object store and remote RPC bindings — a
    cheap amplification lever. Results are reused for ``ttl`` seconds and
    concurrent callers share one in-flight probe.
    """

    def __init__(
        self,
        probe: Callable[[], Awaitable[dict[str, HealthResult]]],
        ttl: float,
    ) -> None:
        self._probe = probe
        self._ttl = ttl
        self._value: dict[str, HealthResult] | None = None
        self._at = 0.0
        self._inflight: asyncio.Task[dict[str, HealthResult]] | None = None

    async def get(self) -> dict[str, HealthResult]:
        now = time.monotonic()
        if self._ttl > 0 and self._value is not None and now - self._at < self._ttl:
            return self._value
        task = self._inflight
        if task is None or task.done():
            task = asyncio.create_task(self._refresh())
            self._inflight = task
        try:
            # One disconnected probe caller must not cancel all other callers.
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight is task:
                self._inflight = None

    async def _refresh(self) -> dict[str, HealthResult]:
        self._value = await self._probe()
        self._at = time.monotonic()
        return self._value


def _install_ops_endpoints(application: FastAPI, core_facade: CoreFacade) -> None:
    # The kernel's ops routes have no user subject: public probes, or a
    # system credential (the internal / metrics token).
    _install_probe_endpoints(application, core_facade)
    # Every other ops route is gated by a service credential (the internal /
    # metrics token: @authenticates(kind="service")).
    _install_diagnostics_endpoints(application, core_facade)
    _install_contract_endpoints(application, core_facade)

    @application.get("/metrics", dependencies=[Depends(require_metrics_access)])
    async def metrics() -> Response:  # pragma: no cover - scraped externally
        """Prometheus metrics exposition endpoint."""
        data = render_metrics()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def _requested_modules(application: FastAPI) -> list[str]:
    summary = getattr(application.state, "module_summary", None)
    explicit = getattr(summary, "explicit", None)
    if explicit is not None:
        return list(explicit)
    return list(getattr(settings, "sandloom_modules", []) or [])


def _failed_modules(application: FastAPI) -> list[str]:
    summary = getattr(application.state, "module_summary", None)
    return [f.name for f in getattr(summary, "failed", []) or []]


def _install_probe_endpoints(application: FastAPI, core_facade: CoreFacade) -> None:
    """``/health/simple`` (liveness), ``/health`` and ``/ready``."""
    health_cache = _HealthCache(
        lambda: aggregate_health(core_facade),
        ttl=float(getattr(settings, "health_cache_ttl_seconds", 3.0)),
    )

    async def _peer_probe() -> dict[str, HealthResult]:
        return {"services": await check_remote_service_bindings(core_facade)}

    # Required remote peers feed readiness ONLY (never liveness), cached with
    # the same TTL/single-flight as the local probes.
    peer_cache = _HealthCache(
        _peer_probe, ttl=float(getattr(settings, "health_cache_ttl_seconds", 3.0))
    )

    probe = _route_policy.public(reason="liveness / readiness probe (no caller)")

    @application.get("/health/simple", dependencies=[probe])
    async def simple_health() -> dict[str, str]:  # pragma: no cover
        return {"status": "ok"}

    @application.get("/health", dependencies=[probe])
    async def health(request: Request) -> Response:  # pragma: no cover
        results = await health_cache.get()
        status = overall_status(results)
        # Public callers get statuses only; check details (which may contain
        # hostnames, paths, driver errors) require the internal token.
        if has_internal_access(request.headers.get("X-Internal-Token")):
            checks: dict[str, object] = {k: asdict(v) for k, v in results.items()}
        else:
            checks = {k: {"status": v.status} for k, v in results.items()}
        body: dict[str, object] = {"status": status, "checks": checks}
        return JSONResponse(status_code=200 if status == "ok" else 503, content=body)

    @application.get("/ready", dependencies=[probe])
    async def readiness(request: Request) -> Response:  # pragma: no cover
        # Readiness = infrastructure health + required modules + at least one
        # business module when any was requested + every required
        # side-effect/projection subscription actively consuming + every
        # remote binding explicitly marked ``"required": true``.
        results, peers = await asyncio.gather(health_cache.get(), peer_cache.get())
        status = overall_status(results)
        peers_status = overall_status(peers)
        required = _required_modules(core_facade)
        missing_required = [r for r in required if r not in core_facade.loaded_modules]
        # A process asked to serve modules but serving none (every module
        # failed and none was required, e.g. an SANDLOOM_MODULES typo) must never
        # look ready: it would answer 404 for its whole API.
        no_modules_loaded = bool(_requested_modules(application)) and not (
            core_facade.loaded_modules
        )
        consumer_runtime = getattr(application.state, "consumer_runtime", None)
        consumer_snapshot = (
            consumer_runtime.snapshot() if consumer_runtime is not None else None
        )
        required_consumers_unavailable = (
            list(consumer_snapshot.required_unavailable)
            if consumer_snapshot is not None
            else []
        )
        # A durable token revocation this process could not apply yet (its
        # applier retries forever): revoked tokens may still pass where the
        # cache is reachable — degrade readiness (never liveness) until it is.
        await _revocation.reconcile_pending_revocations()
        revocations = _revocation.pending_revocations()
        revocations_pending = revocations["count"] > 0
        relay: dict[str, Any] = {"enabled": False}
        local_relay = getattr(application.state, "relay_task", None)
        if local_relay is not None:
            from sandloom.core.tasks.relay import relay_health_status  # noqa: PLC0415 - cold: the relay registers its metrics on import; only a process that runs it may expose them

            relay = relay_health_status(local_relay)
        relay_unavailable = bool(relay.get("enabled")) and (
            not relay.get("running")
            or relay.get("stalled")
            or relay.get("circuit_open")
        )
        ready = (
            status == "ok"
            and peers_status == "ok"
            and not missing_required
            and not no_modules_loaded
            and not required_consumers_unavailable
            and not revocations_pending
            and not relay_unavailable
        )
        body: dict[str, object] = {
            "status": "ok" if ready else "degraded",
            "health_status": status,
            "required_services_status": peers_status,
            "revocations_pending": revocations_pending,
        }
        # The deployment topology (module and consumer names) is internal:
        # only callers holding the internal token see it.
        if has_internal_access(request.headers.get("X-Internal-Token")):
            body.update(
                {
                    "loaded_modules": core_facade.loaded_modules,
                    "failed_modules": _failed_modules(application),
                    "no_modules_loaded": no_modules_loaded,
                    "required_modules": required,
                    "missing_required": missing_required,
                    "required_consumers_unavailable": required_consumers_unavailable,
                    "health_checks": {
                        name: asdict(result) for name, result in results.items()
                    },
                    "relay": relay,
                    "revocations_pending_count": int(revocations["count"]),
                    "revocations_pending_oldest_seconds": round(
                        revocations["oldest_seconds"], 3
                    ),
                    "required_services": asdict(peers["services"])
                    if "services" in peers
                    else None,
                }
            )
        return JSONResponse(status_code=200 if ready else 503, content=body)


def _saga_diagnostics(core_facade: CoreFacade) -> list[dict[str, Any]]:
    runtime = getattr(core_facade, "saga_runtime", None)
    registry = getattr(runtime, "registry", None)
    return registry.describe() if registry is not None else []


def _install_diagnostics_endpoints(
    application: FastAPI, core_facade: CoreFacade
) -> None:
    """Internal ``/diagnostics/*`` (internal token)."""

    @application.get(
        "/diagnostics/core", dependencies=[Depends(require_internal_access)]
    )
    async def core_diagnostics(
        request: Request,
    ) -> dict[str, object]:  # pragma: no cover
        """Lightweight internal diagnostics: module load summary + bus counters."""
        summary = getattr(request.app.state, "module_summary", None)
        summary_block: dict[str, object] | None = None
        if summary is not None:
            try:
                summary_block = summary.to_dict()
            except Exception:  # pragma: no cover  # noqa: BLE001 - diagnostics render without a broken summary
                summary_block = None
        planner_block: list[dict[str, object]] = []
        if getattr(core_facade, "_sandloom_declared_operations", None):
            planner_block = _planner.diagnostics(core_facade)
        return {
            "modules": summary_block,
            # profile, tier counts and active overrides (sandloom.core.config.tiers)
            "configuration": tiers.diagnostics(settings),
            "event_bus_metrics": get_event_bus_metrics(),
            # registered saga definitions and versions (ADR-0008)
            "sagas": _saga_diagnostics(core_facade),
            # declared operations: the executor the kernel picked, and why
            "planner": {
                "optimized_executor_enabled": bool(settings.planner_optimized_executor),
                "operations": planner_block,
            },
        }

    @application.get(
        "/diagnostics/consumers",
        dependencies=[Depends(require_internal_access)],
    )
    async def consumer_diagnostics() -> dict[str, object]:  # pragma: no cover
        """Report subscription state for local side-effect/projection consumers."""
        runtime = getattr(application.state, "consumer_runtime", None)
        if runtime is None:
            return {
                "ready": True,
                "required_unavailable": [],
                "consumers": [],
            }
        snapshot = runtime.snapshot()
        return {
            "ready": snapshot.ready,
            "required_unavailable": list(snapshot.required_unavailable),
            "consumers": [asdict(item) for item in snapshot.consumers],
        }

    @application.get(
        "/diagnostics/services",
        dependencies=[Depends(require_internal_access)],
    )
    async def services_diagnostics() -> dict[str, object]:  # pragma: no cover
        """Report local service-registry contracts and configured remote bindings."""
        local = set(core_facade.service_registry.list())
        remote = set(settings.service_bindings)
        # Internal-only: probe EVERY remote binding (required or not) so an
        # operator sees which peers are reachable without affecting /ready.
        reachability = await check_remote_service_bindings(
            core_facade, only_required=False
        )
        return {
            "local": sorted(local),
            "remote_bindings": sorted(remote),
            "remote_only": sorted(remote - local),
            "local_overrides_remote": sorted(local & remote),
            "required_bindings": sorted(
                name
                for name, binding in settings.service_bindings.items()
                if binding.get("required") is True
            ),
            "reachability": asdict(reachability),
        }

    @application.get(
        "/diagnostics/infra",
        dependencies=[Depends(require_internal_access)],
    )
    async def infra_diagnostics() -> dict[str, object]:  # pragma: no cover
        """Report configured infrastructure instances + per-instance health."""
        infra = core_facade.infra
        health = await infra.health_all()
        return {
            "infra": infra.describe(),
            "health": {k: asdict(v) for k, v in health.items()},
        }


def _install_contract_endpoints(application: FastAPI, core_facade: CoreFacade) -> None:
    """Internal ``/contracts/*`` (internal token)."""

    @application.get(
        "/contracts/schemas",
        dependencies=[Depends(require_internal_access)],
    )
    async def list_contract_schemas() -> dict[str, object]:  # pragma: no cover
        """Return current event schema registry snapshot (internal)."""
        registry = core_facade.get_schema_registry()
        schemas: list[SchemaEntry] = registry.list()
        return {
            "schemas": [asdict(s) for s in schemas],
            "count": len(schemas),
        }

    @application.get(
        "/contracts/persistence",
        dependencies=[Depends(require_internal_access)],
    )
    async def contract_persistence_status() -> dict[str, object]:  # pragma: no cover
        """Return schema registry persistence status (internal diagnostics)."""
        registry = core_facade.get_schema_registry()
        return {
            "persistence_path": getattr(registry, "persistence_path", None),
            "loaded_from_disk": getattr(registry, "loaded_from_disk", False),
            "count": len(registry.list()),
        }

    @application.post(
        "/contracts/diff", dependencies=[Depends(require_internal_access)]
    )
    async def contract_diff(
        req: ContractDiffRequest,
    ) -> dict[str, object]:  # pragma: no cover
        """Dry-run classification of a prospective schema change.

        Does NOT mutate the registry. Returns classification report if the
        channel exists; otherwise a marker that a new schema would be created.
        """

        try:
            _validators.validate_channel_name(req.channel)
        except _validators.ContractValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        registry = core_facade.get_schema_registry()
        existing_schema = registry.get(req.channel)
        version = int(req.channel.split(".")[-1][1:])

        if existing_schema and existing_schema.schema:
            report = classify_schema_change(
                existing_schema.schema, req.new_schema, req.channel, version
            )
            return {"existing": True, "report": report.to_dict()}

        return {"existing": False, "new": True, "breaking": False}


class ContractDiffRequest(BaseModel):  # Phase J: dry-run diff endpoint
    channel: str
    new_schema: dict[str, object]


__all__ = [
    "ConfigurationError",
    "ContractDiffRequest",
    "configuration_problems",
    "create_app",
    "enforce_configuration",
]
