"""Framework Celery worker entrypoint (owned by the kernel, not a module).

Run the worker / beat for ANY Sandloom application — with or without the bundled
``identity`` module::

    celery -A sandloom.core.worker worker --loglevel=INFO
    celery -A sandloom.core.worker beat --loglevel=INFO

How the worker app is assembled (no backend is constructed here):

1. **Boot the composition root** — the same one the API and ``sandloom-migrate``
   use (``SANDLOOM_COMPOSITION_ROOT``, default ``sandloom.main``; an ``sandloom new``
   project
   points it at ``<app>.asgi``). Importing it runs ``create_app()``, which loads
   exactly the ``SANDLOOM_MODULES`` set and binds the active core, so task bodies can
   use ``db_session()`` / ``publish_event`` like a request handler.
2. **Resolve the Celery app through the capability registry** —
   ``core.taskqueue(SANDLOOM_WORKER_TASKQUEUE or "default")``. The taskqueue DRIVER
   is the sole constructor of the Celery app (``test_infra_constructed_only_in
   _drivers``), and the enqueue side (``core.taskqueue().enqueue``) and this
   worker therefore share one app, broker and task namespace.
3. **Discover module tasks** — for every LOADED module ``<pkg>`` the worker
   imports ``<pkg>.tasks`` if it exists. A module declares tasks with Celery's
   app-agnostic ``@shared_task`` (it never builds or imports an app), and may
   expose ``setup_periodic_tasks(sender, **kwargs)`` which is connected to the
   app's ``on_after_finalize`` so ``beat`` schedules it.

A module that is not in ``SANDLOOM_MODULES`` contributes no tasks, so a split
deployment runs one worker per service with that service's module set.

4. **One event loop per worker process** — task bodies are synchronous, so
   they run coroutines through ``sandloom.core.di.run_async`` (never
   ``asyncio.run``): the async engine pools are per-process and loop-bound, so
   a fresh loop per task breaks the pool on the 2nd task. The worker wires
   ``worker_process_init`` (fresh loop after fork) and
   ``worker_process_shutdown`` (dispose the engines/caches on that loop, then
   close it) — see :mod:`sandloom.core.async_runner`.
5. **One beat per broker and module set** — beat schedules only while it
   holds a fenced lease in the cache (:mod:`sandloom.core.beat_lease`); a second
   beat stands by and takes over when the first dies.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import math
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from sandloom.core import async_runner, cpu_quota
from sandloom.core.beat_lease import install_beat_lease
from sandloom.core.composition import COMPOSITION_ROOT_ENV, CompositionRootError
from sandloom.core.composition import load_composition_core as _load_root_core
from sandloom.core.config.common import strict_profile_active
from sandloom.core.gc_tuning import install_celery_hooks as install_celery_gc_hooks
from sandloom.core.tasks import periodic as _periodic

logger = logging.getLogger(__name__)

#: Logical taskqueue instance whose app the worker consumes (default ``default``).
TASKQUEUE_INSTANCE_ENV = "SANDLOOM_WORKER_TASKQUEUE"
#: Submodule of a business module that holds its ``@shared_task`` functions.
TASKS_SUBMODULE = "tasks"
#: Optional hook in ``<pkg>.tasks`` that registers periodic (beat) tasks.
PERIODIC_HOOK = _periodic.PERIODIC_HOOK
#: Prefork children (default: cgroup CPUs, capped at MAX_DEFAULT_WORKER_CONCURRENCY).
WORKER_CONCURRENCY_ENV = "WORKER_CONCURRENCY"
MAX_DEFAULT_WORKER_CONCURRENCY = 2
#: Recycle a prefork child above this resident size (KiB) after its task.
WORKER_MAX_MEMORY_ENV = "WORKER_MAX_MEMORY_PER_CHILD"
#: Recycle a prefork child after this many tasks.
WORKER_MAX_TASKS_ENV = "WORKER_MAX_TASKS_PER_CHILD"
#: Liveness heartbeat file (``python -m sandloom.core.worker healthcheck``).
HEARTBEAT_FILE_ENV = "WORKER_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT_FILE = "/tmp/sandloom-worker-heartbeat"  # noqa: S108 - default liveness file; WORKER_HEARTBEAT_FILE overrides
HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_MAX_AGE_SECONDS = 60.0


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


def _resolve_celery_app(core: Any, instance: str) -> Any:
    """Return the Celery app backing taskqueue ``instance`` (driver-built)."""
    provider = core.taskqueue(instance)
    backend = provider.raw_backend()
    if inspect.isawaitable(backend):
        # Worker boot runs before any event loop exists; the accessor is a pure
        # getter, so a throwaway loop is enough.
        async def _await() -> Any:
            return await backend

        backend = asyncio.run(_await())
    if not (hasattr(backend, "on_after_finalize") and hasattr(backend, "task")):
        raise WorkerConfigurationError(
            f"taskqueue instance {instance!r} (driver "
            f"{getattr(provider, 'driver', '?')!r}) is not backed by a Celery "
            "app; the framework worker needs the `celery` taskqueue driver"
        )
    return backend


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
    from sandloom.core.lifecycle import run_shutdown  # noqa: PLC0415 - cold: `python -m sandloom.core.worker healthcheck` imports this module and must stay cheap; the relational stack loads only in a booted worker

    await run_shutdown(core)


def default_worker_concurrency(cpus: float | None = None) -> int:
    """Prefork children when ``WORKER_CONCURRENCY`` is unset.

    Celery defaults to ``os.cpu_count()`` — the HOST's cores, not the
    container's CPU quota — and every prefork child holds a full copy of the
    composition root (all modules, pools). A 0.5-CPU / 384 MiB worker on a
    10-core host therefore forked 10 children and was OOM-killed in a loop.
    Derive it from the cgroup quota like the web workers
    (:mod:`sandloom.core.cpu_quota`) and cap it small.
    """
    effective = cpu_quota.available_cpus() if cpus is None else cpus
    return max(1, min(math.ceil(effective), MAX_DEFAULT_WORKER_CONCURRENCY))


def _positive_int_env(env: dict[str, str] | os._Environ[str], key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkerConfigurationError(
            f"{key} must be a positive integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise WorkerConfigurationError(f"{key} must be a positive integer, got {raw!r}")
    return value


def configure_worker_sizing(app: Any, env: dict[str, str] | None = None) -> None:
    """Apply container-aware sizing to the Celery app's config.

    - ``worker_concurrency``: ``WORKER_CONCURRENCY`` or
      :func:`default_worker_concurrency` (an explicit ``--concurrency`` on the
      command line still wins over app config);
    - ``worker_max_memory_per_child`` (KiB): ``WORKER_MAX_MEMORY_PER_CHILD``,
      recycling a child that grew past it after its current task;
    - ``worker_max_tasks_per_child``: ``WORKER_MAX_TASKS_PER_CHILD``.
    """
    source = os.environ if env is None else env
    conf = app.conf
    conf.worker_concurrency = (
        _positive_int_env(source, WORKER_CONCURRENCY_ENV)
        or default_worker_concurrency()
    )
    max_mem = _positive_int_env(source, WORKER_MAX_MEMORY_ENV)
    if max_mem is not None:
        conf.worker_max_memory_per_child = max_mem
    max_tasks = _positive_int_env(source, WORKER_MAX_TASKS_ENV)
    if max_tasks is not None:
        conf.worker_max_tasks_per_child = max_tasks


def configure_worker_reliability(app: Any) -> None:
    """Framework delivery defaults of the worker's Celery app.

    - ``task_ignore_result=True``: a task stores no result unless it opts out
      (``@shared_task(ignore_result=False)``) — the framework never reads
      results, and each stored one is a backend write + TTL key;
    - ``worker_prefetch_multiplier=1``: a child reserves one message at a
      time, so a long task does not strand a prefetched backlog behind it (and
      a crashed child loses at most its own message's ack).

    ``acks_late`` stays OFF globally (Celery's at-most-once default: a task is
    acked when it starts). An IDEMPOTENT task should opt in to at-least-once
    with ``@shared_task(acks_late=True, reject_on_worker_lost=True)``: it is
    acked only after it finished, and redelivered if the worker process dies
    mid-task — only safe when running it twice is harmless.
    """
    conf = app.conf
    conf.task_ignore_result = True
    conf.worker_prefetch_multiplier = 1


def heartbeat_path(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(HEARTBEAT_FILE_ENV) or "").strip() or DEFAULT_HEARTBEAT_FILE


def install_heartbeat(app: Any, path: str | None = None) -> None:
    """Touch a heartbeat file from the worker's own timer (liveness probe).

    The timer runs in the worker's main process event machinery, so the file
    goes stale when the worker is wedged or dead — not merely when a child is
    busy. ``python -m sandloom.core.worker healthcheck`` checks its age without
    booting the app (no broker round-trip, no second copy of the composition
    root inside the container's memory limit, unlike ``celery inspect ping``).
    """
    from celery import bootsteps  # noqa: PLC0415 - cold: `python -m sandloom.core.worker healthcheck` runs without Celery; only the worker boot assembles bootsteps

    if getattr(app, "_sandloom_heartbeat_installed", False):
        return  # one heartbeat step per app, however often it is assembled
    target = Path(path or heartbeat_path())

    class _Heartbeat(bootsteps.StartStopStep):  # type: ignore[misc]
        requires = {("celery.worker.components:Timer")}  # noqa: RUF012 - celery bootstep API

        def __init__(self, parent: Any, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self.tref: Any = None
            self.stopped = False

        def _beat(self, *_: Any) -> None:
            if self.stopped:
                return
            try:
                target.touch()
            except OSError:  # pragma: no cover - read-only fs
                logger.warning("[worker] cannot touch heartbeat %s", target)

        def start(self, parent: Any) -> None:
            self._beat()
            self.tref = parent.timer.call_repeatedly(
                HEARTBEAT_INTERVAL_SECONDS, self._beat, (), priority=10
            )

        def stop(self, parent: Any) -> None:
            self.stopped = True
            if self.tref is not None:
                self.tref.cancel()
            target.unlink(missing_ok=True)

    app.steps["worker"].add(_Heartbeat)
    app._sandloom_heartbeat_installed = True


def heartbeat_is_fresh(
    path: str | None = None,
    *,
    max_age: float = HEARTBEAT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    """True when the heartbeat file exists and was touched within ``max_age``."""
    try:
        mtime = os.path.getmtime(path or heartbeat_path())
    except OSError:
        return False
    return ((time.time() if now is None else now) - mtime) <= max_age


def verify_worker_infra(core: Any) -> None:
    """Run the database security checks the API lifespan runs (worker boot).

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
    from sandloom.core.production_profile import enforce_runtime  # noqa: PLC0415 - cold: `python -m sandloom.core.worker healthcheck` imports this module and must stay cheap; the relational stack loads only in a booted worker

    strict = strict_profile_active(getattr(core, "settings", None))
    if not checks and not strict:
        return

    async def _run() -> None:
        for check in checks:
            await check()
        if strict:  # PRODUCTION_PROFILE=multi_tenant_strict: RLS coverage
            await enforce_runtime(core)

    asyncio.run(_run())


def build_worker_app(core: Any | None = None, *, instance: str | None = None) -> Any:
    """Assemble the worker's Celery app for the loaded module set."""
    global _teardown_core
    core = core if core is not None else load_composition_core()
    verify_worker_infra(core)
    name = instance or os.getenv(TASKQUEUE_INSTANCE_ENV) or "default"
    app = _resolve_celery_app(core, name)
    _teardown_core = core
    configure_worker_sizing(app)
    configure_worker_reliability(app)
    install_heartbeat(app)
    install_beat_lease(app, core, instance=name)
    install_celery_gc_hooks()
    async_runner.add_loop_cleanup(dispose_worker_infra)
    async_runner.install_celery_hooks()
    for module in discover_task_modules(list(core.loaded_modules)):
        hook = getattr(module, PERIODIC_HOOK, None)
        if callable(hook):
            # weak=False: the hook is a module-level function, but a strong ref
            # keeps it alive regardless of how the module is reloaded.
            app.on_after_finalize.connect(hook, weak=False)
    return app


_app: Any | None = None


def get_worker_app() -> Any:
    """Return the process-wide worker app (assembled once, lazily)."""
    global _app
    if _app is None:
        _app = build_worker_app()
    return _app


def __getattr__(name: str) -> Any:
    # ``celery -A sandloom.core.worker`` looks up ``app`` (or ``celery``); building
    # lazily keeps a plain ``import sandloom.core.worker`` free of side effects.
    if name in {"app", "celery", "celery_app"}:
        return get_worker_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "COMPOSITION_ROOT_ENV",
    "PERIODIC_HOOK",
    "TASKQUEUE_INSTANCE_ENV",
    "TASKS_SUBMODULE",
    "WorkerConfigurationError",
    "build_worker_app",
    "configure_worker_reliability",
    "configure_worker_sizing",
    "default_worker_concurrency",
    "dispose_worker_infra",
    "verify_worker_infra",
    "discover_task_modules",
    "get_worker_app",
    "heartbeat_is_fresh",
    "install_heartbeat",
    "load_composition_core",
]


def main(argv: list[str] | None = None) -> int:
    """``python -m sandloom.core.worker healthcheck`` → 0 when the heartbeat is fresh.

    Deliberately boots nothing (no composition root, no broker): a container
    healthcheck must be cheap and must not double the worker's memory.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["healthcheck"]:
        print("usage: python -m sandloom.core.worker healthcheck", file=sys.stderr)
        return 2
    if heartbeat_is_fresh():
        return 0
    print(f"worker heartbeat {heartbeat_path()} missing or stale", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - container healthcheck entry
    raise SystemExit(main())
