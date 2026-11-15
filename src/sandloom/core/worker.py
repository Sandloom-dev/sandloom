"""Framework worker boot (owned by the kernel, not a module).

Run the worker / scheduler for ANY Sandloom application — with or without the
bundled ``identity`` module::

    sandloom worker --loglevel=INFO
    sandloom beat --loglevel=INFO

The kernel does not run a message loop. It boots the application and hands the
task table to the taskqueue driver's host (:class:`~sandloom.core.infra.ports.
taskqueue.WorkerHost`, reached through ``TaskQueueProvider.worker_host()``); this
module imports no task-queue backend.

1. **Boot the composition root** — the same one the API and ``sandloom-migrate``
   use (``SANDLOOM_COMPOSITION_ROOT``, default ``sandloom.main``; an ``sandloom new``
   project points it at ``<app>.asgi``). Importing it runs ``create_app()``, which
   loads exactly the ``SANDLOOM_MODULES`` set and binds the active core, so task
   bodies can use ``db_session()`` / ``publish_event`` like a request handler.
2. **Resolve the host through the capability registry** —
   ``core.taskqueue(SANDLOOM_WORKER_TASKQUEUE or "default").worker_host()``. The
   enqueue side and the worker therefore share one driver instance, broker and
   task namespace.
3. **Discover module tasks** — :class:`KernelWorkerBoot` (the host's
   :class:`~sandloom.core.infra.ports.taskqueue.WorkerBoot`) imports ``<pkg>.tasks``
   for every LOADED module that has one. A module declares tasks with ``@task`` and
   ``@periodic`` of ``sandloom.sdk.tasks`` (it never builds or imports a queue);
   the kernel derives the :class:`~sandloom.core.infra.ports.taskqueue.TaskTable`
   from the declarations and the host registers it.

A module that is not in ``SANDLOOM_MODULES`` contributes no tasks, so a split
deployment runs one worker per service with that service's module set.

4. **One event loop per worker process** — an ``async def`` task body runs
   through ``sandloom.core.di.run_async`` (never ``asyncio.run``), a sync body
   directly: the async engine pools are per-process and loop-bound, so a fresh
   loop per task breaks the pool on the 2nd task. The host calls
   ``on_process_start`` (a fresh loop after a fork) and ``on_process_stop``
   (dispose the engines/caches on that loop, then close it) — see
   :mod:`sandloom.core.async_runner`.
5. **One scheduler per broker and module set** — it schedules only while it holds
   a fenced lease of the ``lease`` instance ``beat``
   (:mod:`sandloom.core.infra.fenced_lease`): the kernel resolves the lease store
   and its name and passes them to the host; a second scheduler stands by and
   takes over when the first dies.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import math
import os
import sys
from collections.abc import Coroutine, Iterable
from types import ModuleType
from typing import Any, TypeVar, cast

from sandloom.core import cpu_quota
from sandloom.core.composition import COMPOSITION_ROOT_ENV, CompositionRootError
from sandloom.core.composition import load_composition_core as _load_root_core
from sandloom.core.config.common import strict_profile_active
from sandloom.core.config.settings import settings
from sandloom.core.gc_tuning import freeze_before_fork
from sandloom.core.infra.errors import InfraError
from sandloom.core.infra.fenced_lease import (
    LeaseNotSharedError,
    broker_url_of,
    resolve_beat_lease,
)
from sandloom.core.infra.ports.taskqueue import TaskTable, WorkerHost
from sandloom.core.infra.registry import CapabilityUnavailable, InfraRegistryError
from sandloom.core.tasks.registry import build_task_table, run_task

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _async_runner() -> ModuleType:
    """:mod:`sandloom.core.async_runner`, imported where a worker process needs
    it: it pulls in the unit of work and the web framework, which
    ``sandloom worker check`` (a healthcheck every 30 s) must not load."""
    return importlib.import_module("sandloom.core.async_runner")


#: Logical taskqueue instance whose host the worker runs (default ``default``).
TASKQUEUE_INSTANCE_ENV = "SANDLOOM_WORKER_TASKQUEUE"
#: Submodule of a business module that holds its ``@task`` functions.
TASKS_SUBMODULE = "tasks"
#: Worker processes (default: cgroup CPUs, capped at MAX_DEFAULT_WORKER_CONCURRENCY).
WORKER_CONCURRENCY_ENV = "WORKER_CONCURRENCY"
MAX_DEFAULT_WORKER_CONCURRENCY = 2


class WorkerConfigurationError(CompositionRootError):
    """The worker cannot be assembled from the current configuration."""


def load_composition_core(root_name: str | None = None) -> Any:
    """Import the composition root and return its ``CoreFacade``.

    The SAME resolution as ``sandloom-migrate`` and the ``sandloom`` operational CLIs
    (:func:`sandloom.core.composition.load_composition_core`): the root exposes
    ``core`` or ``app.state.core``.
    """
    try:
        return _load_root_core(root_name)
    except CompositionRootError as exc:
        raise WorkerConfigurationError(str(exc)) from exc


def taskqueue_instance() -> str:
    """The taskqueue instance the worker and the scheduler run
    (``SANDLOOM_WORKER_TASKQUEUE``, default ``default``)."""
    return os.getenv(TASKQUEUE_INSTANCE_ENV) or "default"


def resolve_worker_host(core: Any, instance: str) -> tuple[Any, WorkerHost]:
    """Return ``(provider, host)``: taskqueue ``instance`` and the host its
    driver runs."""
    provider = core.taskqueue(instance)
    opener = getattr(provider, "worker_host", None)
    if not callable(opener):
        raise WorkerConfigurationError(
            f"taskqueue instance {instance!r} (driver "
            f"{getattr(provider, 'driver', '?')!r}) offers no worker host; the "
            "framework worker needs a taskqueue driver with `worker_host()`"
        )
    host: WorkerHost = opener()
    return provider, host


def discover_task_modules(module_names: Iterable[str]) -> list[ModuleType]:
    """Import ``<pkg>.tasks`` for each loaded module that has one."""
    found: list[ModuleType] = []
    for pkg in module_names:
        candidate = f"{pkg}.{TASKS_SUBMODULE}"
        try:
            spec = importlib.util.find_spec(candidate)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            continue
        found.append(importlib.import_module(candidate))
        logger.info("[worker] registered tasks from %s", candidate)
    return found


#: The core whose infrastructure this worker process tears down at shutdown.
_teardown_core: Any | None = None


async def dispose_worker_infra() -> None:
    """Close this worker process's pools on the loop that opened them.

    Runs the composition root's own shutdown tasks — exactly the API
    lifespan's teardown (RPC client, default engine, default cache client,
    every registry-owned instance), in reverse registration order — so the
    worker never reaches a concrete driver itself. Each task is best-effort
    (``run_shutdown`` logs and continues).
    """
    core = _teardown_core
    if core is None or not hasattr(core, "shutdown_tasks"):
        return
    from sandloom.core.lifecycle import run_shutdown  # noqa: PLC0415 - cold: the relational stack loads only in a booted worker

    await run_shutdown(core)


def default_worker_concurrency(cpus: float | None = None) -> int:
    """Worker processes when ``WORKER_CONCURRENCY`` is unset.

    A prefork pool defaults to ``os.cpu_count()`` — the HOST's cores, not the
    container's CPU quota — and every child holds a full copy of the
    composition root (all modules, pools). A 0.5-CPU / 384 MiB worker on a
    10-core host therefore forked 10 children and was OOM-killed in a loop.
    Derive it from the cgroup quota like the web workers
    (:mod:`sandloom.core.cpu_quota`) and cap it small.
    """
    effective = cpu_quota.available_cpus() if cpus is None else cpus
    return max(1, min(math.ceil(effective), MAX_DEFAULT_WORKER_CONCURRENCY))


def worker_concurrency(env: dict[str, str] | os._Environ[str] | None = None) -> int:
    """``WORKER_CONCURRENCY``, else :func:`default_worker_concurrency`."""
    source = os.environ if env is None else env
    raw = (source.get(WORKER_CONCURRENCY_ENV) or "").strip()
    if not raw:
        return default_worker_concurrency()
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkerConfigurationError(
            f"{WORKER_CONCURRENCY_ENV} must be a positive integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise WorkerConfigurationError(
            f"{WORKER_CONCURRENCY_ENV} must be a positive integer, got {raw!r}"
        )
    return value


def verify_worker_infra(core: Any) -> None:
    """Run the database checks the API lifespan runs (worker boot): the
    schema of a database built from the models, and the security checks.

    The worker never enters the ASGI lifespan, so ``aopen_all`` does not run
    here; a ``tenant_rls`` database whose application role could escape
    Row-Level Security must still refuse to start. Each database provider
    exposing ``verify_tenant_rls`` checks on throw-away connections (no pooled
    connection survives this loop into forked pool processes). Raises the
    provider's error (``TenantRLSConfigError``) — the worker does not boot.
    """
    try:
        infra = core.infra
    except (AttributeError, RuntimeError):
        return
    providers = [
        infra.provider("database", name) for name in infra.instances("database")
    ]
    checks = [
        p.verify_tenant_rls
        for p in providers
        if "tenant_rls" in p.features() and hasattr(p, "verify_tenant_rls")
    ]
    from sandloom.core.production_profile import enforce_runtime  # noqa: PLC0415 - cold: the relational stack loads only in a booted worker

    # A database that takes its schema from the models (SQLite) is built or
    # checked here too, never recreated: a worker refuses one whose tables
    # differ from the models (SANDLOOM-B028). The provider releases its pool
    # after, before the worker forks.
    schemas = [
        p.ensure_model_schema for p in providers if hasattr(p, "ensure_model_schema")
    ]
    strict = strict_profile_active(getattr(core, "settings", None))
    if not checks and not strict and not schemas:
        return

    async def _run() -> None:
        for ensure in schemas:
            await ensure(None, recreate=False)
        for check in checks:
            await check()
        if strict:  # PRODUCTION_PROFILE=multi_tenant_strict: RLS coverage
            await enforce_runtime(core)

    asyncio.run(_run())


def check_task_table_features(table: Any, provider: Any, instance: str) -> None:
    """Refuse a task table the instance's driver cannot serve (SANDLOOM-B037).

    The load-time derivation reads ``at_least_once=True`` only as a literal; the
    table is what really runs. A task with ``at_least_once`` needs the feature
    ``redelivery`` and a periodic one ``periodic``: without them the worker would
    start and silently run at most once, or never schedule.
    """
    features = provider.features()
    missing: dict[str, list[str]] = {}
    for name, spec in table.tasks.items():
        if spec.at_least_once and "redelivery" not in features:
            missing.setdefault("redelivery", []).append(name)
    for entry in table.periodic:
        if "periodic" not in features:
            missing.setdefault("periodic", []).append(entry.id)
    if not missing:
        return
    detail = "; ".join(
        f"feature {feature!r} for {', '.join(sorted(names))}"
        for feature, names in sorted(missing.items())
    )
    raise CapabilityUnavailable(
        f"taskqueue instance {instance!r} (driver {provider.driver!r}) lacks "
        f"{detail}: the loaded modules' tasks need it",
        fix="select in INFRA a taskqueue driver that advertises the feature, "
        "or remove at_least_once / the @periodic task",
    )


class KernelWorkerBoot:
    """The kernel's :class:`~sandloom.core.infra.ports.taskqueue.WorkerBoot`.

    ``boot`` is the application boot of a worker process (database checks, task
    discovery, the feature check); ``run`` runs one message through the kernel's
    task layer; the process hooks own the persistent loop and the GC freeze.
    The host calls them; this class imports no task-queue backend.
    """

    def __init__(self, core: Any, provider: Any, instance: str) -> None:
        self._core = core
        self._provider = provider
        self._instance = instance

    def boot(self) -> TaskTable:
        global _teardown_core
        verify_worker_infra(self._core)
        _teardown_core = self._core
        _async_runner().add_loop_cleanup(dispose_worker_infra)
        modules = discover_task_modules(list(self._core.loaded_modules))
        table = build_task_table(m.__name__ for m in modules)
        check_task_table_features(table, self._provider, self._instance)
        return table

    def run(self, task: str, payload: bytes, *, task_id: str) -> None:
        run_task(task, payload)

    def before_fork(self) -> None:
        freeze_before_fork()

    def on_process_start(self) -> None:
        _async_runner().reset_loop()

    def on_process_stop(self) -> None:
        _async_runner().shutdown_loop()

    def run_coroutine(self, coro: Coroutine[Any, Any, T]) -> T:
        return cast(T, _async_runner().run_async(coro))


def run_worker(
    *,
    queues: list[str] | None = None,
    concurrency: int | None = None,
    loglevel: str = "INFO",
    core: Any | None = None,
    instance: str | None = None,
) -> int:
    """``sandloom worker``: boot the composition and run the driver's worker
    host (its process model is a driver option, not an argument); the process
    exit code."""
    core = core if core is not None else load_composition_core()
    name = instance or taskqueue_instance()
    provider, host = resolve_worker_host(core, name)
    boot = KernelWorkerBoot(core, provider, name)
    return host.run_worker(
        boot,
        queues=queues,
        concurrency=concurrency if concurrency is not None else worker_concurrency(),
        loglevel=loglevel,
    )


def run_scheduler(
    *,
    loglevel: str = "INFO",
    state_file: str | None = None,
    core: Any | None = None,
    instance: str | None = None,
) -> int:
    """``sandloom beat``: boot the composition and run the driver's scheduler
    under the beat lease; the process exit code.

    The kernel resolves the lease store, the lease name (broker, taskqueue
    instance and module set) and the production check; a lease it cannot grant
    refuses the scheduler before anything is scheduled (exit 1).
    """
    core = core if core is not None else load_composition_core()
    name = instance or taskqueue_instance()
    provider, host = resolve_worker_host(core, name)
    try:
        lease, lease_name = resolve_beat_lease(
            core,
            broker_url=broker_url_of(core, name),
            instance=name,
            shared_required=settings.is_prod,
        )
    except (LeaseNotSharedError, InfraRegistryError, InfraError) as exc:
        logger.critical("[beat] refusing to schedule: %s", exc)
        return 1
    return host.run_scheduler(
        KernelWorkerBoot(core, provider, name),
        lease=lease,
        lease_name=lease_name,
        lease_ttl_seconds=settings.beat_lease_ttl_seconds,
        loglevel=loglevel,
        state_file=state_file,
    )


def check_worker(
    infra: Any, timeout_seconds: float, *, instance: str | None = None
) -> tuple[bool, str]:
    """``sandloom worker check``: ask the driver's host whether a worker runs.

    It boots no composition root (no module, no database, no broker round
    trip): ``infra`` is a registry built from the settings alone
    (:func:`sandloom.core.infra.tool_infra.build_tool_infra`), whose
    providers are lazy; the host of the worker's taskqueue instance answers.
    """
    name = instance or taskqueue_instance()
    provider = infra.provider("taskqueue", name)
    host: WorkerHost = provider.worker_host()
    result = host.check(timeout_seconds=timeout_seconds)
    if result.status == "ok":
        return True, ""
    reason = (result.details or {}).get("error")
    return False, str(reason or "worker check failed")


__all__ = [
    "COMPOSITION_ROOT_ENV",
    "TASKQUEUE_INSTANCE_ENV",
    "TASKS_SUBMODULE",
    "KernelWorkerBoot",
    "WorkerConfigurationError",
    "check_task_table_features",
    "check_worker",
    "default_worker_concurrency",
    "dispose_worker_infra",
    "verify_worker_infra",
    "discover_task_modules",
    "load_composition_core",
    "resolve_worker_host",
    "run_scheduler",
    "run_worker",
    "taskqueue_instance",
    "worker_concurrency",
]


if __name__ == "__main__":
    # Tombstone, not an alias: ``python -m sandloom.core.worker healthcheck`` used to
    # be the container probe. Importing the module and exiting 0 would turn a kept
    # old probe into a liveness check that never fails: exit non-zero, run nothing.
    sys.exit(
        "sandloom.core.worker has no command line any more: use `sandloom worker` "
        "(run the worker), `sandloom beat` (run the scheduler) or "
        "`sandloom worker check` (the container probe)"
    )
