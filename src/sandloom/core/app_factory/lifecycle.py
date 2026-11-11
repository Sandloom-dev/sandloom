"""The application's lifecycle: health checks, infrastructure startup and
shutdown tasks, the token-signing role check and the ASGI lifespan (startup,
relay, consumers, drain), bound to the app's own core."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI

import sandloom.core.auth.tokens as _tokens
import sandloom.core.observability.tracing as _tracing
import sandloom.core.sql_alchemy.model_schema as _model_schema
import sandloom.core.tasks.pool_metrics as _pool_metrics
import sandloom.core.unit_of_work.dispatch as _dispatch
from sandloom.core.app_factory.config import outbox_relay_problem
from sandloom.core.config.common import strict_profile_active
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult, ModuleLoadSummary
from sandloom.core.drivers.bootstrap import (
    close_default_cache,
    close_default_database,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.lifecycle import (
    run_shutdown,
    run_startup,
)
from sandloom.core.orm import Base
from sandloom.core.production_profile import (
    enforce_runtime as enforce_production_profile,
)
from sandloom.core.rpc import (
    check_remote_contract_compat,
    describe_remote_bindings,
)
from sandloom.core.runtime import use_active_core
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

    async def describe_bindings() -> None:
        """Describe every remote binding to seed the proxy's describe cache
        (ADR-0014 K6): no describe ``GET`` on a first request."""
        await describe_remote_bindings(core_facade)

    if not settings.service_bindings:
        return
    if settings.contract_compat_check != "off":
        # The compat probe also describes (and seeds) every remote binding.
        core_facade.add_startup_task(
            contract_compat_check,
            critical=settings.contract_compat_check == "strict",
            phase="post",
        )
    else:
        core_facade.add_startup_task(describe_bindings, critical=False, phase="post")


def _register_health_checks(core_facade: CoreFacade, infra: InfraRegistry) -> None:
    async def db_health() -> HealthResult:
        return await _capability_health(core_facade, "database")

    async def cache_health() -> HealthResult:
        return await _capability_health(core_facade, "cache")

    async def storage_health() -> HealthResult:
        # The kernel uses no object store: a synthesized one nothing requires
        # is neither built nor probed (database, cache and eventbus are the
        # kernel's own, KERNEL_INSTANCES, and always probed).
        if not infra.in_use("objectstore"):
            return HealthResult(status="ok", details={"status": "unused"})
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
        INFRA so /ready reflects them too. The taskqueue is never probed
        here: readiness owns it (the processes that use it on the request
        path), so ``/health`` opens no broker connection and a broker outage
        never fails it.
        """
        results = await infra.health_all(skip=frozenset({"taskqueue"}))
        failures = {
            key: res.details
            for key, res in results.items()
            if not key.endswith(":default") and res.status != "ok"
        }
        if failures:
            return HealthResult(status="fail", details={"instances": failures})
        return HealthResult(status="ok")

    core_facade.register_health_check("infra", infra_instances_health)


class _BindActiveCore:
    """ASGI middleware: run each request, and the background work it starts,
    with this application's core as the active runtime (``use_active_core``),
    not the process default the last ``create_app`` set."""

    def __init__(self, app: Any, *, core: CoreFacade) -> None:
        self.app = app
        self.core = core

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        with use_active_core(self.core):
            await self.app(scope, receive, send)


def _make_lifespan(
    application: FastAPI,
    core_facade: CoreFacade,
    module_summary: ModuleLoadSummary,
    consumer_runtime: Any,
) -> Callable[[FastAPI], Any]:
    body = _make_lifespan_body(
        application, core_facade, module_summary, consumer_runtime
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
        # Startup, the relay, consumers and shutdown run bound to this app's
        # core; the background tasks they start inherit the binding.
        with use_active_core(core_facade):
            async with body(app):
                yield

    return lifespan


def _make_lifespan_body(
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
