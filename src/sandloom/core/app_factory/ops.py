"""Operational endpoints: liveness, readiness, health, diagnostics, metrics
and the contract endpoints."""

import asyncio
import importlib.util
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict
from typing import Any, Generic, TypeVar

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel

import sandloom.core.app_factory.revocation_applier as _applier
import sandloom.core.auth.revocation as _revocation
import sandloom.core.contracts.validators as _validators
import sandloom.core.route_policy as _route_policy
from sandloom.core import planner as _planner
from sandloom.core.app_factory.lifecycle import _capability_health
from sandloom.core.config import tiers
from sandloom.core.config.settings import settings
from sandloom.core.contracts import SchemaEntry
from sandloom.core.contracts.validators import classify_schema_change
from sandloom.core.descriptors import HealthResult
from sandloom.core.descriptors.module_names import resolve_module_names
from sandloom.core.eventing.bus_metrics import get_event_bus_metrics
from sandloom.core.facade import CoreFacade
from sandloom.core.internal_auth import (
    has_internal_access,
    require_internal_access,
    require_metrics_access,
)
from sandloom.core.lifecycle import (
    aggregate_health,
    overall_status,
)
from sandloom.core.observability.metrics import render_metrics
from sandloom.core.rpc import (
    check_remote_service_bindings,
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


_T = TypeVar("_T")


class _HealthCache(Generic[_T]):
    """Short-TTL, single-flight cache of the aggregated health probes.

    ``/health`` and ``/ready`` are public: without a cache every anonymous hit
    fans out to the database, cache, object store and remote RPC bindings — a
    cheap amplification lever. Results are reused for ``ttl`` seconds and
    concurrent callers share one in-flight probe. Generic over the probe's
    result (the revocation dead-letter read caches its own reading).
    """

    def __init__(
        self,
        probe: Callable[[], Awaitable[_T]],
        ttl: float,
    ) -> None:
        self._probe = probe
        self._ttl = ttl
        self._value: _T | None = None
        self._at = 0.0
        self._inflight: asyncio.Task[_T] | None = None

    async def get(self) -> _T:
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

    async def _refresh(self) -> _T:
        self._value = await self._probe()
        self._at = time.monotonic()
        return self._value


#: Submodule of a business module that declares its tasks and its beat hook
#: (``sandloom.core.worker.TASKS_SUBMODULE``: the worker imports exactly these).
_TASKS_SUBMODULE = "tasks"
#: The taskqueue instance whose tasks the loaded modules enqueue.
_TASKQUEUE_INSTANCE = "default"


def _task_modules(module_names: Iterable[str]) -> list[str]:
    """Loaded modules that register tasks or periodic tasks.

    A module does so in its ``<pkg>.tasks`` submodule, the one the worker
    imports (:func:`sandloom.core.worker.discover_task_modules`); the beat hook
    lives there too. Found with ``find_spec``, so ``tasks`` is not imported.
    """
    found: list[str] = []
    for pkg in module_names:
        try:
            spec = importlib.util.find_spec(f"{pkg}.{_TASKS_SUBMODULE}")
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            found.append(pkg)
    return found


async def _taskqueue_health(core_facade: CoreFacade) -> HealthResult:
    """Open the configured taskqueue and probe it.

    Building the provider is what fails when the driver cannot open (for
    ``celery``: Celery not installed); building sends nothing to the broker.
    Bounded by ``HEALTH_CHECK_TIMEOUT_SECONDS`` like every other probe.
    """
    timeout = settings.health_check_timeout_seconds
    try:
        return await asyncio.wait_for(
            _capability_health(core_facade, "taskqueue", name=_TASKQUEUE_INSTANCE),
            timeout=timeout,
        )
    except TimeoutError:
        return HealthResult(status="fail", details={"error": f"timeout > {timeout}s"})


class _TaskqueueReadiness:
    """The readiness term for the taskqueue (I-20: no silent degradation).

    While a loaded module registers tasks or periodic tasks, a taskqueue that
    cannot open makes the process not ready, naming the capability. With no
    such module the taskqueue is never probed. Readiness only: ``/health``
    never carries it. The probe shares the TTL/single-flight cache of the
    other readiness probes, so a ``/ready`` hit adds no I/O of its own.
    """

    def __init__(self, core_facade: CoreFacade, *, ttl: float) -> None:
        """Bind the core whose loaded modules and taskqueue are checked."""
        self._core = core_facade
        self._cache: _HealthCache[dict[str, HealthResult]] = _HealthCache(
            self._probe, ttl=ttl
        )
        self._loaded: tuple[str, ...] | None = None
        self._task_modules: list[str] = []

    async def _probe(self) -> dict[str, HealthResult]:
        return {"taskqueue": await _taskqueue_health(self._core)}

    def task_modules(self) -> list[str]:
        """The loaded modules that use the taskqueue, looked up once per
        loaded set (fixed once the app serves), not on every probe."""
        loaded = tuple(self._core.loaded_modules)
        if loaded != self._loaded:
            self._task_modules = _task_modules(loaded)
            self._loaded = loaded
        return self._task_modules

    async def check(self) -> tuple[str, dict[str, object] | None]:
        """``(status, internal block)``: status ``ok`` / ``fail`` (the probe's
        own status) or ``unused`` with no block when no loaded module has
        tasks."""
        modules = self.task_modules()
        if not modules:
            return "unused", None
        result = (await self._cache.get())["taskqueue"]
        return result.status, {
            "capability": "taskqueue",
            "instance": _TASKQUEUE_INSTANCE,
            "task_modules": modules,
            **asdict(result),
        }


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

    # The taskqueue feeds readiness ONLY, and only while a loaded module uses it.
    taskqueue_readiness = _TaskqueueReadiness(
        core_facade, ttl=float(getattr(settings, "health_cache_ttl_seconds", 3.0))
    )
    # The revocation / block consumers' backlog, dead letters and state
    # (ADR-0012 decision 5, A9). The state and lag are the runtime's
    # snapshot (no I/O); the dead-letter read shares the probes' TTL and
    # single flight: one read per TTL however many /ready calls arrive.
    revocation_probe = _applier.RevocationReadinessProbe(core_facade, settings)
    revocation_dead_letters = _HealthCache(
        revocation_probe.read_dead_letters,
        ttl=float(getattr(settings, "health_cache_ttl_seconds", 3.0)),
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
        # remote binding explicitly marked ``"required": true`` + a usable
        # taskqueue while a loaded module registers tasks.
        results, peers, (taskqueue_status, taskqueue) = await asyncio.gather(
            health_cache.get(), peer_cache.get(), taskqueue_readiness.check()
        )
        status = overall_status(results)
        peers_status = overall_status(peers)
        taskqueue_unavailable = taskqueue_status not in ("ok", "unused")
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
        revocation_consumers = await revocation_probe(
            consumer_runtime, dead_letters=revocation_dead_letters.get
        )
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
            and revocation_consumers.ready
            and not relay_unavailable
            and not taskqueue_unavailable
        )
        body: dict[str, object] = {
            "status": "ok" if ready else "degraded",
            "health_status": status,
            "required_services_status": peers_status,
            "revocations_pending": revocations_pending,
            # ``ok`` | ``fail`` | ``unused`` (no loaded module registers tasks).
            "taskqueue_status": taskqueue_status,
            "revocation_consumers_ready": revocation_consumers.ready,
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
                    "revocation_consumers": {
                        "reasons": list(revocation_consumers.reasons),
                        "lag_seconds": {
                            name: round(age, 3)
                            for name, age in revocation_consumers.lag_seconds.items()
                        },
                        "dead_letters": dict(revocation_consumers.dead_letters),
                    },
                    "required_services": asdict(peers["services"])
                    if "services" in peers
                    else None,
                    "taskqueue": taskqueue,
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
