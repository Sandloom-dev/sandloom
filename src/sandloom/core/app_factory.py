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
import html
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import HTTPException
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse
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
import sandloom.core.sql_alchemy.dbapi as _dbapi
import sandloom.core.sql_alchemy.model_schema as _model_schema
import sandloom.core.tasks.pool_metrics as _pool_metrics
import sandloom.core.unit_of_work.dispatch as _dispatch
from sandloom.core import contract_cache_shadow, rpc_binding, rpc_identity, rpc_pair_mac
from sandloom.core import planner as _planner
from sandloom.core._internal.db_provider import get_db
from sandloom.core.auth.tokens import jwt_key_material_problem
from sandloom.core.config import accepted_risks as _risks
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
from sandloom.core.guarantee_lattice import (
    Mechanism,
    nested_unit_label,
    tenant_isolation_label,
)
from sandloom.core.guarantee_windows import enforce_guarantee_windows
from sandloom.core.http.error_handlers import register_error_handlers
from sandloom.core.http.middlewares import register_middlewares
from sandloom.core.idempotency.durable import (
    install_durable_capture,
    validate_money_guards,
)
from sandloom.core.infra.active import set_active_infra
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InfraConfigError,
    database_instance_url,
    load_infra_config,
)
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
from sandloom.core.orm import Base
from sandloom.core.production_profile import config_problems, tenant_scoped_tables
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
from sandloom.core.sql_alchemy.portable import (
    development_only,
    dialect_of_url,
    row_security_of_url,
    serial_writers,
    sql_dialect,
    unrendered_dialect,
    unrendered_partial_indexes,
)
from sandloom.core.sql_alchemy.read_only_policy import read_only_sessions_problem
from sandloom.core.sql_alchemy.tenant_rls import row_security_problem


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


async def report_unreachable_database(core_facade: CoreFacade) -> None:
    """Development: say once, in one line, that the database cannot be
    reached (the dev services are stopped) and how to start it. The app
    still starts: a request answers 503 until the database is back."""
    result = await _capability_health(core_facade, "database")
    if result.status == "ok":
        return
    where = ""
    with suppress(ValueError):
        parts = urlsplit(settings.database_url)
        if parts.hostname:
            where = f" at {parts.hostname}" + (f":{parts.port}" if parts.port else "")
    logging.getLogger("sandloom.app").warning(
        "PostgreSQL%s fails its health check: when `sandloom dev` runs it, start "
        "it with `uv run sandloom dev up` (`dev run` does too); otherwise check "
        "DATABASE_URL and the server's log   [SANDLOOM-R014]",
        where,
    )


class ConfigurationError(SystemExit):
    """The configuration fails a check that runs at boot.

    A ``SystemExit`` like a ``Settings`` load failure: the entry point
    (ASGI import, worker, ``sandloom-migrate``, the ``sandloom`` CLI) stops with the
    message and exit status 1 before serving or touching anything.
    """


def configured_database_urls(cfg: Any) -> list[tuple[str, str]]:
    """``(label, url)`` for every database URL of ``cfg``: each instance's
    URL, its ``system_url`` option, and ``DATABASE_SYSTEM_URL`` for the
    default instance. Empty when ``INFRA`` is invalid (refused where the
    infrastructure is built)."""
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return []
    found: list[tuple[str, str]] = []
    for name, instance in sorted(databases.items()):
        url = database_instance_url(instance.options, cfg)
        if url:
            found.append((name, url))
        system = instance.options.get("system_url") or (
            getattr(cfg, "database_system_url", None)
            if name == DEFAULT_INSTANCE
            else None
        )
        if system:
            found.append((f"{name} system channel", str(system)))
    return found


def sql_dialect_problem(cfg: Any) -> str | None:
    """A configured SQL database whose dialect has no SQL dialect renderer.

    Modules write the portable forms of ``sandloom.sdk.data`` and the database's
    renderer turns them into SQL (SANDLOOM-B024); without one the first table
    or statement that uses a form would fail, so the process does not start.
    """
    missing: list[str] = []
    for name, url in configured_database_urls(cfg):
        dialect = unrendered_dialect(url)
        if dialect is not None:
            missing.append(f"{name} ({dialect})")
    if not missing:
        return None
    return (
        "no SQL dialect renderer for the database(s) "
        + ", ".join(missing)
        + " [SANDLOOM-B024]: install the driver package that provides one"
    )


def row_security_config_problem(cfg: Any) -> str | None:
    """A database instance that asks for row security its database lacks.

    ``tenant_rls``, ``system_url`` and ``require_system_channel`` need a
    renderer with row security (SANDLOOM-B026). Build-free: the options and
    the URL's renderer only, so every entry point refuses before it starts.
    """
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return None  # refused where the infrastructure is built
    problems: list[str] = []
    for name, instance in sorted(databases.items()):
        url = database_instance_url(instance.options, cfg)
        if not url:
            continue
        system_url = (
            getattr(cfg, "database_system_url", None)
            if name == DEFAULT_INSTANCE
            else None
        )
        problem = row_security_problem(
            name, str(url), instance.options, system_url=system_url
        )
        if problem is not None:
            problems.append(problem)
    return "; ".join(problems) or None


def dbapi_config_problem(cfg: Any) -> str | None:
    """A configured database its DBAPI adapter cannot open, as the adapter
    states it (SQLite: in memory, or aiosqlite not installed, SANDLOOM-B032).
    Build-free: the URLs and the installed packages only."""
    problems: list[str] = []
    for name, url in configured_database_urls(cfg):
        problem = _dbapi.dbapi_config_problem(url)
        if problem is not None:
            problems.append(f"database/{name}: {problem}")
    return "; ".join(problems) or None


#: The accepted risk that admits a development database in production.
DEVELOPMENT_DATABASE_RISK = "development_database_in_prod"


def development_database_config_problem(cfg: Any) -> str | None:
    """A production process on a database whose renderer serves development
    and tests only (SQLite, SANDLOOM-B029): refused unless the deployment
    accepts the risk ``development_database_in_prod``, and always under
    ``PRODUCTION_PROFILE=multi_tenant_strict``. Build-free."""
    if not getattr(cfg, "is_prod", False):
        return None
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return None  # refused where the infrastructure is built
    strict = strict_profile_active(cfg)
    if not strict and _risks.accepts(cfg, DEVELOPMENT_DATABASE_RISK):
        return None
    found = []
    for name, instance in sorted(databases.items()):
        url = database_instance_url(instance.options, cfg)
        if url and development_only(dialect_of_url(url)):
            found.append(f"database/{name} ({dialect_of_url(url)})")
    if not found:
        return None
    fix = (
        "PRODUCTION_PROFILE=multi_tenant_strict refuses it even when accepted: "
        "use a production database"
        if strict
        else "use a production database, or accept the risk with "
        f'SANDLOOM_ACCEPTED_RISKS=["{DEVELOPMENT_DATABASE_RISK}"]'
    )
    return (
        f"{', '.join(found)} serve(s) development and tests only, and "
        f"ENVIRONMENT=prod [SANDLOOM-B029]: {fix}"
    )


def read_only_sessions_config_problem(cfg: Any) -> str | None:
    """``DB_READ_ONLY_SESSIONS`` on a default database with nothing to refuse
    a write: no read-only transactions in its renderer and no client
    read-only policy in its DBAPI adapter (SANDLOOM-B031). Build-free."""
    if not getattr(cfg, "db_read_only_sessions", False):
        return None
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return None  # refused where the infrastructure is built
    instance = databases.get(DEFAULT_INSTANCE)
    url = database_instance_url(instance.options if instance else None, cfg)
    if not url:
        return None
    return read_only_sessions_problem(DEFAULT_INSTANCE, url)


def application_enforced_isolation(
    infra: InfraRegistry, metadata: Any
) -> list[tuple[str, str]]:
    """``(instance, label)`` for each SQL database whose renderer has no row
    security, when ``metadata`` maps a ``TenantScoped`` table: their
    isolation there is the ORM guard only. The label is the guarantee
    lattice's, computed from the instance's features.

    Which instance hosts which table is not known before a database is
    reached, so any mapped ``TenantScoped`` table counts for every such
    instance. A PostgreSQL instance without ``tenant_rls`` is
    application-enforced too, by the operator's choice: it is not reported.
    """
    found: list[tuple[str, str]] = []
    mapped = set(getattr(metadata, "tables", {}))
    if not mapped.intersection(tenant_scoped_tables()):
        return found
    for name in infra.instances("database"):
        served = infra.sql_dialects("database", name)
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if served is None or not served[1] or not url:
            continue
        if sql_dialect(dialect_of_url(str(url))) is None:
            continue  # SANDLOOM-B024's refusal
        if row_security_of_url(str(url)) is not None:
            continue
        features = infra.advertised_features("database", name) or frozenset()
        label = tenant_isolation_label(Mechanism.from_features(features))
        if label is not None:
            found.append((name, label))
    return found


def warn_application_enforced_isolation(
    infra: InfraRegistry, metadata: Any, log: logging.Logger
) -> None:
    """One startup warning per :func:`application_enforced_isolation`
    instance: said aloud, never weakened silently (spec infra-agnostic,
    decision 3)."""
    for name, label in application_enforced_isolation(infra, metadata):
        log.warning(
            "database/%s has no row security: %s (the ORM guard only; raw SQL "
            "is not confined to the bound tenant). The strict production "
            "profile refuses it.",
            name,
            label,
            extra={"event": "tenant_isolation.application_enforced"},
        )


def nested_units_join_caller(infra: InfraRegistry) -> list[tuple[str, str]]:
    """``(instance, label)`` for each SQL database whose writers are serial
    (its renderer's ``serial_writers``: SQLite): a nested write unit of the
    same task there (a local contract call, a saga step's unit, a child
    ``open_uow()``) is a SAVEPOINT of its caller and commits with it. The
    label is the guarantee lattice's."""
    found: list[tuple[str, str]] = []
    for name in infra.instances("database"):
        served = infra.sql_dialects("database", name)
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if served is None or not served[1] or not url:
            continue
        label = nested_unit_label(
            Mechanism(serial_writers=serial_writers(dialect_of_url(str(url))))
        )
        if label is not None:
            found.append((name, label))
    return found


def warn_nested_units_join_caller(infra: InfraRegistry, log: logging.Logger) -> None:
    """One startup warning per :func:`nested_units_join_caller` instance:
    the weaker contract atomicity is said aloud, never silently."""
    for name, label in nested_units_join_caller(infra):
        log.warning(
            "database/%s has one writer at a time: a nested write unit of the "
            "same task (a local contract call, a saga step, a child unit) runs "
            "as a SAVEPOINT of its caller and %s (an outer rollback undoes its "
            "rows, outbox events and command record). Development and tests "
            "only.",
            name,
            label,
            extra={"event": "unit.nested_savepoint"},
        )


def sql_storage_problems(infra: InfraRegistry, metadata: Any) -> list[str]:
    """The SQL databases a process cannot use as configured.

    Each database instance's URL must name a dialect its driver declares it
    serves (``sql_dialects=`` at driver registration, SANDLOOM-B024), and every
    :func:`~sandloom.sdk.data.partial_index` must have a condition on each of
    those dialects (SANDLOOM-R015): otherwise it would index every row.
    """
    problems: list[str] = []
    dialects: set[str] = set()
    for name in infra.instances("database"):
        served = infra.sql_dialects("database", name)
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if served is None or not served[1] or not url:
            continue
        driver, names = served
        dialect = dialect_of_url(str(url))
        dialects.add(dialect)
        if dialect not in names:
            problems.append(
                f"database {name!r}: driver {driver!r} serves {sorted(names)}, "
                f"not {dialect!r} [SANDLOOM-B024]"
            )
    unrestricted = unrendered_partial_indexes(metadata, dialects)
    if unrestricted:
        problems.append(
            "partial indexes with no condition on their database: "
            + ", ".join(unrestricted)
            + " [SANDLOOM-R015]"
        )
    return problems


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
    * a SQL dialect renderer for every configured database
      (:func:`sql_dialect_problem`), with row security where an instance
      asks for it (:func:`row_security_config_problem`, SANDLOOM-B026), a
      database its DBAPI adapter can open (:func:`dbapi_config_problem`,
      SANDLOOM-B032 on SQLite), no development-only database in production
      (:func:`development_database_config_problem`, SANDLOOM-B029), and a
      guard for ``DB_READ_ONLY_SESSIONS``
      (:func:`read_only_sessions_config_problem`, SANDLOOM-B031);
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
        sql_dialect_problem,
        row_security_config_problem,
        dbapi_config_problem,
        development_database_config_problem,
        read_only_sessions_config_problem,
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
    # Every model is declared now: each SQL database is one its driver serves,
    # and every partial index can be restricted there (B024, R015).
    storage_problems = sql_storage_problems(infra, Base.metadata)
    if storage_problems:
        raise ConfigurationError(INVALID_CONFIGURATION + "; ".join(storage_problems))
    warn_application_enforced_isolation(infra, Base.metadata, main_logger)
    warn_nested_units_join_caller(infra, main_logger)
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
    if settings.environment.value == "dev":

        async def report_unreachable_dev_database() -> None:
            await report_unreachable_database(core_facade)

        core_facade.add_startup_task(report_unreachable_dev_database, phase="post")

    application.router.lifespan_context = _make_lifespan(
        application, core_facade, module_summary, consumer_runtime
    )

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


async def ensure_model_schemas(
    infra: InfraRegistry, metadata: Any, *, recreate: bool | None = None
) -> list[str]:
    """:func:`sandloom.core.sql_alchemy.model_schema.ensure_model_schemas`;
    ``recreate`` defaults to ``DB_RECREATE_ON_MODEL_CHANGE`` (set by
    ``sandloom dev run``), never in prod."""
    if recreate is None:
        recreate = bool(settings.db_recreate_on_model_change) and not settings.is_prod
    return await _model_schema.ensure_model_schemas(
        infra, metadata, settings, recreate=recreate
    )


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

    async def infra_model_schema() -> None:
        """A database that takes its schema from the models (SQLite) gets
        it now, every module declared; one whose tables differ from the
        models refuses to start, or is set aside and built fresh under
        ``sandloom dev run`` (SANDLOOM-B028)."""
        await ensure_model_schemas(infra, Base.metadata)

    core_facade.add_startup_task(infra_model_schema, critical=True, phase="infra")

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


_START_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto;
  padding: 0 1rem; color: #1f1d1a; background: #faf8f5; }}
@media (prefers-color-scheme: dark) {{ body {{ color: #ece8e1; background: #1c1a17; }}
  a {{ color: #e0b07a; }} }}
code {{ font-size: 0.9em; }}
</style></head><body>
<h1>{title}</h1>
<p>The app is running. This page is served outside production only.</p>
<ul>
{links}
</ul>
<p>Next:</p>
<ul>
{commands}
</ul>
</body></html>
"""


#: The caracal of ``docs/assets/sandloom-mark.svg`` in the brand colours
#: (``website/assets/brand.css``: #8b6841, #d8b892 on a dark scheme).
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">'
    "<style>path{fill:#8b6841}"
    "@media (prefers-color-scheme:dark){path{fill:#d8b892}}</style>"
    '<path fill-rule="evenodd" d="M17 3 22.5 7.5 27 28Q40 22 53 28L57.5 7.5 63 3 64 '
    "29 70 42.5 64 59 52 69 40 74 28 69 16 59 10 42.5 16 29ZM19.5 17 23.5 31.5 "
    "18 34ZM60.5 17 56.5 31.5 62 34ZM20.5 41.8C25.8 40.3 31.3 41.4 34 45.6C32.4 "
    "48.4 29.5 49 26.6 47.7C23.4 46.3 21.4 44.4 20.5 41.8ZM59.5 41.8C54.2 40.3 "
    "48.7 41.4 46 45.6C47.6 48.4 50.5 49 53.4 47.7C56.6 46.3 58.6 44.4 59.5 "
    "41.8ZM34.5 54.5Q40 52.5 45.5 54.5L42 58.5 41 59V61.5L45.5 63.2 44.9 64.6 40 "
    '62.8 35.1 64.6 34.5 63.2 39 61.5V59L38 58.5Z"/></svg>'
)


#: ``/docs`` and ``/redoc`` outside production look like sandloom.dev: the
#: stylesheet and the brand fonts ship in ``sandloom/core/http/api_docs`` and
#: are served from ``/docs/assets/`` (never a font service).
_API_DOCS = Path(__file__).parent / "http" / "api_docs"
_API_DOCS_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".woff2": "font/woff2",
}

#: ReDoc's two themes: the site's fonts, accent and surfaces. ReDoc has no dark
#: mode of its own: api_docs.js redraws it with the other theme on a switch.
_REDOC_CODE = {"fontFamily": "Hack Nerd Font Mono, ui-monospace, monospace"}
_REDOC_FONTS: dict[str, Any] = {
    "fontFamily": "Manrope, system-ui, sans-serif",
    "headings": {"fontFamily": "Manrope, system-ui, sans-serif", "fontWeight": "700"},
    "code": _REDOC_CODE,
}
_REDOC_THEMES = {
    "light": {
        "colors": {
            "primary": {"main": "#8b6841"},
            "text": {"primary": "#24272a", "secondary": "#686c6f"},
            "border": {"dark": "#dedfda", "light": "#eeede8"},
        },
        "typography": _REDOC_FONTS,
        "sidebar": {"backgroundColor": "#f7f6f2", "textColor": "#24272a"},
        "rightPanel": {"backgroundColor": "#24272a"},
    },
    "dark": {
        "colors": {
            "primary": {"main": "#d8b892"},
            "text": {"primary": "#edeee9", "secondary": "#a5aaa8"},
            "border": {"dark": "#303337", "light": "#222528"},
        },
        "typography": {
            **_REDOC_FONTS,
            "code": {
                **_REDOC_CODE,
                "color": "#d8b892",
                "backgroundColor": "#222528",
            },
        },
        "schema": {"nestedBackground": "#191b1d", "typeNameColor": "#a5aaa8"},
        "sidebar": {"backgroundColor": "#191b1d", "textColor": "#edeee9"},
        "rightPanel": {"backgroundColor": "#0b0c0d"},
    },
}

#: The switch in the header: system, light, dark (api_docs.css shows one icon).
_THEME_SWITCH = (
    '<button class="sl-theme" type="button" aria-label="Theme">'
    '<svg class="sl-i-system" viewBox="0 0 24 24"><rect x="3" y="4" width="18" '
    'height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>'
    '<svg class="sl-i-light" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
    'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    '<svg class="sl-i-dark" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4 '
    '8 8 0 1 0 20 14.5Z"/></svg></button>'
)


def _docs_favicon() -> str:
    return "data:image/svg+xml," + quote(_FAVICON_SVG)


def _docs_bar(application: FastAPI, current: str) -> str:
    """The site's header with the app's name: the console (when the app
    serves it), Swagger UI or ReDoc, and the Sandloom docs."""
    mark = re.sub(r"<style>.*?</style>", "", _FAVICON_SVG)
    console = any(
        str(getattr(route, "path", "")).startswith("/admin")
        for route in application.routes
    )
    links = []
    if console:
        links.append(("/admin/", "Console", ""))
    links += [("/docs", "Swagger UI", ""), ("/redoc", "ReDoc", "")]
    nav = "".join(
        f'<a href="{href}" class="{cls}"'
        + (' aria-current="page"' if href == current else "")
        + f">{label}</a>"
        for href, label, cls in links
    )
    return (
        f'<header class="sl-bar"><span class="sl-brand">{mark}'
        f"{html.escape(application.title)}</span>"
        '<span class="sl-pill">API</span>'
        f"<nav>{nav}"
        '<a class="sl-out sl-wide" href="https://sandloom.dev/docs/" target="_blank"'
        f' rel="noopener">Sandloom docs</a></nav>{_THEME_SWITCH}</header>'
    )


def _docs_head() -> str:
    return (
        '<link rel="stylesheet" href="/docs/assets/api_docs.css">'
        '<script src="/docs/assets/api_docs.js"></script>'
    )


def _install_branded_docs(application: FastAPI) -> None:
    """Swagger UI at ``/docs`` and ReDoc at ``/redoc``, as FastAPI serves them
    (same paths, the OAuth2 redirect, ``root_path``), in the sandloom.dev look:
    the site's header, fonts, colours and dark mode. Plain Starlette routes,
    like FastAPI's own: no route policy, not in the schema."""
    openapi_url = application.openapi_url or "/openapi.json"
    redirect_url = application.swagger_ui_oauth2_redirect_url

    async def swagger_ui(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        page = get_swagger_ui_html(
            openapi_url=root + openapi_url,
            title=f"{application.title} API",
            swagger_favicon_url=_docs_favicon(),
            oauth2_redirect_url=root + redirect_url if redirect_url else None,
            init_oauth=application.swagger_ui_init_oauth,
            swagger_ui_parameters={
                "displayRequestDuration": True,
                "filter": True,
                "persistAuthorization": True,  # outside production only
                "syntaxHighlight": {"theme": "tomorrow-night"},
                **(application.swagger_ui_parameters or {}),
            },
        )
        body = bytes(page.body).decode()
        body = body.replace("</head>", _docs_head() + "\n</head>", 1)
        body = body.replace("<body>", "<body>\n" + _docs_bar(application, "/docs"), 1)
        return HTMLResponse(body)

    async def swagger_ui_redirect(request: Request) -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    async def redoc(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        page = get_redoc_html(
            openapi_url=root + openapi_url,
            title=f"{application.title} API",
            redoc_favicon_url=_docs_favicon(),
            with_google_fonts=False,
        )
        body = bytes(page.body).decode()
        body = body.replace("</head>", _docs_head() + "\n</head>", 1)
        body = body.replace("<body>", "<body>\n" + _docs_bar(application, "/redoc"), 1)
        # ReDoc drawn by script, so the switch can redraw it in the other theme.
        tag = re.search(r"<redoc [^>]*></redoc>", body)
        if tag is not None:
            spec = json.dumps(root + openapi_url)
            themes = json.dumps(_REDOC_THEMES)
            body = body.replace(
                tag.group(0),
                '<div id="redoc"></div><script>window.slRedoc = '
                f"{{spec: {spec}, themes: {themes}}};</script>",
                1,
            )
            body = body.replace(
                "</body>",
                "<script>(() => { const r = window.slRedoc; const draw = () => "
                "Redoc.init(r.spec, {scrollYOffset: '.sl-bar', theme: "
                "window.slTheme.dark() ? r.themes.dark : r.themes.light}, "
                "document.getElementById('redoc')); draw(); "
                "document.addEventListener('sl-theme', draw); })();</script>\n</body>",
                1,
            )
        return HTMLResponse(body)

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        file = _API_DOCS / name
        kind = _API_DOCS_TYPES.get(file.suffix)
        if kind is None or "/" in name or not file.is_file():
            return Response(status_code=404)
        # The stylesheet changes with the framework: revalidated each time.
        cache = "no-cache" if file.suffix == ".css" else "public, max-age=86400"
        return Response(
            file.read_bytes(), media_type=kind, headers={"Cache-Control": cache}
        )

    application.add_route("/docs", swagger_ui, include_in_schema=False)
    application.add_route("/docs/assets/{name}", asset, include_in_schema=False)
    if redirect_url:
        application.add_route(
            redirect_url, swagger_ui_redirect, include_in_schema=False
        )
    application.add_route("/redoc", redoc, include_in_schema=False)


def _install_favicon(application: FastAPI) -> None:
    """``GET /favicon.ico`` outside production, unless the app serves it:
    the caracal, so a browser on the start page, the docs or the console
    stops logging a 404. Production leaves it to the app."""
    if settings.is_prod:
        return
    for route in application.routes:
        if getattr(route, "path", None) == "/favicon.ico":
            return

    @application.get(
        "/favicon.ico",
        include_in_schema=False,
        dependencies=[
            _route_policy.public(reason="development favicon (never in production)")
        ],
    )
    async def favicon() -> Response:
        return Response(
            _FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


def _install_start_page(application: FastAPI) -> None:
    """``GET /`` outside production, unless the app routes ``/`` itself: the
    address ``sandloom dev run`` prints answers with links to the console,
    the API docs and ``/health`` instead of a 404. Production leaves ``/``
    unrouted."""
    if settings.is_prod:
        return
    for route in application.routes:
        if getattr(route, "path", None) == "/" and "GET" in (
            getattr(route, "methods", None) or ()
        ):
            return
    # The console is whatever serves /admin/ (the kernel names no module).
    console = any(
        str(getattr(route, "path", "")).startswith("/admin")
        for route in application.routes
    )
    links = []
    if console:
        links.append('<li><a href="/admin/">The admin console</a> (/admin/)</li>')
    if settings.docs_exposed:
        links.append('<li><a href="/docs">The API docs</a> (/docs)</li>')
    links.append('<li><a href="/health">Health</a> (/health)</li>')
    commands = []
    if console:
        commands.append(
            "<li><code>uv run sandloom admin list</code> (the administrators; "
            "<code>uv run sandloom admin create --email &lt;address&gt;</code> "
            "adds one)</li>"
        )
    commands += [
        "<li><code>uv run sandloom add module notes</code> (a module)</li>",
        "<li><code>uv run sandloom test</code> (tests and every gate)</li>",
    ]
    page = _START_PAGE.format(
        title=html.escape(application.title),
        links="\n".join(links),
        commands="\n".join(commands),
    )

    @application.get(
        "/",
        include_in_schema=False,
        dependencies=[
            _route_policy.public(reason="development start page (never in production)")
        ],
    )
    async def start_page() -> HTMLResponse:
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})


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
