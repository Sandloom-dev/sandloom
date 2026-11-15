"""The Celery worker host of the built-in ``celery`` taskqueue driver.

Everything Celery-specific about running tasks lives here, behind the port
(:class:`~sandloom.driverkit.WorkerHost`): the kernel names no Celery object.

* :class:`CeleryWorkerHost` — ``run_worker`` registers the kernel's task table on
  the Celery app and runs ``celery worker`` over it; ``run_scheduler`` runs
  ``celery beat`` only while the process holds the lease the kernel resolved;
  ``check`` reads the worker's heartbeat file.
* Celery bootsteps and signals: the heartbeat step (liveness probe, no broker
  round trip), ``worker_init`` (:meth:`WorkerBoot.before_fork`),
  ``worker_process_init`` / ``worker_process_shutdown``
  (:meth:`WorkerBoot.on_process_start` / ``on_process_stop``) and ``beat_init``
  (the lease wrapper of the scheduler).
* :func:`get_default_app`, the sole builder of the process-wide ``default``
  Celery app, which the provider (``celery_taskqueue``) and this host share.

**The importable Celery app.** ``celery -A sandloom.core.drivers.celery_worker
inspect|call|status`` and Flower use the module attribute ``app`` (also
``celery`` and ``celery_app``): the ``default`` app, built lazily from
``BROKER_URL``, with no composition root and no task registered. It is the
driver's own interface, not a shim; it moves to ``sandloom_celery.app`` with the
package (``idk-pkg-celery``).

**Process options.** The port carries the log level and the scheduler's state
file (``state_file``: Celery beat's ``--schedule``, default ``celerybeat-schedule``
in the working directory); the Celery-specific sizing knobs
``WORKER_MAX_MEMORY_PER_CHILD`` and ``WORKER_MAX_TASKS_PER_CHILD`` and the
liveness file ``WORKER_HEARTBEAT_FILE`` are read here.
"""

from __future__ import annotations

import functools
import importlib
import logging
import os
import time
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import orjson

from sandloom.driverkit import (
    FencedLease,
    HealthResult,
    InfraError,
    InvalidArgument,
    LeaseStore,
    PeriodicSpec,
    TaskTable,
    Timeout,
    Unavailable,
    WorkerBoot,
)

logger = logging.getLogger(__name__)

#: Recycle a prefork child above this resident size (KiB) after its task.
WORKER_MAX_MEMORY_ENV = "WORKER_MAX_MEMORY_PER_CHILD"
#: Recycle a prefork child after this many tasks.
WORKER_MAX_TASKS_ENV = "WORKER_MAX_TASKS_PER_CHILD"
#: Liveness heartbeat file (``sandloom worker check``).
HEARTBEAT_FILE_ENV = "WORKER_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT_FILE = "/tmp/sandloom-worker-heartbeat"  # noqa: S108 - default liveness file; WORKER_HEARTBEAT_FILE overrides
HEARTBEAT_INTERVAL_SECONDS = 10.0
#: The broker of the importable app (the kernel's ``BROKER_URL`` setting).
BROKER_URL_ENV = "BROKER_URL"

#: Process-wide ``default`` Celery app, built lazily, and the ``(broker,
#: backend)`` it was built from.
_default_app: Any | None = None
_default_app_urls: tuple[str, str | None] | None = None


def _celery_module() -> Any:
    """``celery``, imported where a command needs it: ``sandloom worker check``
    and a publish-only process import none of it."""
    return importlib.import_module("celery")


def _signals() -> Any:
    return importlib.import_module("celery.signals")


def _apply_framework_defaults(app: Any) -> Any:
    """Framework Celery defaults (shared by the enqueue side and the worker).

    ``task_ignore_result``: tasks store no result unless they opt out with
    ``@shared_task(ignore_result=False)`` — nothing in the framework reads
    task results, and storing each one costs a backend write + TTL key.
    """
    app.conf.task_ignore_result = True
    return app


def get_default_app(broker: str, backend: str | None) -> Any:
    """Return the process-wide ``default`` Celery app (built once, from the
    ``default`` instance's ``broker`` and ``backend``)."""
    global _default_app, _default_app_urls
    if _default_app is None:
        _default_app = _apply_framework_defaults(
            _celery_module().Celery("project", broker=broker, backend=backend)
        )
        _default_app_urls = (broker, backend)
    return _default_app


def default_app_matches(broker: str, backend: str | None) -> bool:
    """Whether the ``default`` instance may use the process-wide app: it is not
    built yet, or was built from these URLs."""
    return _default_app is None or _default_app_urls == (broker, backend)


def build_named_app(name: str, broker: str, backend: str | None) -> Any:
    """A Celery app of a named instance (its own broker and namespace)."""
    return _apply_framework_defaults(
        _celery_module().Celery(f"sandloom-{name}", broker=broker, backend=backend)
    )


def __getattr__(name: str) -> Any:
    # ``celery -A sandloom.core.drivers.celery_worker`` looks up ``app`` (or
    # ``celery``); building lazily keeps a plain import free of side effects.
    if name in {"app", "celery", "celery_app"}:
        if _default_app is not None:
            return _default_app
        broker = (os.environ.get(BROKER_URL_ENV) or "").strip()
        if not broker:
            raise AttributeError(
                f"the importable Celery app needs {BROKER_URL_ENV} (the broker "
                "of the default taskqueue instance)"
            )
        return get_default_app(broker, None)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------- app settings


def _positive_int_env(env: Any, key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise InvalidArgument(
            f"{key} must be a positive integer, got {raw!r}", capability="taskqueue"
        )
    return value


def configure_worker_sizing(
    app: Any, *, concurrency: int | None, env: dict[str, str] | None = None
) -> None:
    """Apply the worker's sizing to the Celery app's config.

    - ``worker_concurrency``: ``concurrency`` (the kernel resolves it: the
      ``--concurrency`` flag, ``WORKER_CONCURRENCY`` or the CPU quota);
    - ``worker_max_memory_per_child`` (KiB): ``WORKER_MAX_MEMORY_PER_CHILD``,
      recycling a child that grew past it after its current task;
    - ``worker_max_tasks_per_child``: ``WORKER_MAX_TASKS_PER_CHILD``.
    """
    source = os.environ if env is None else env
    conf = app.conf
    if concurrency is not None:
        conf.worker_concurrency = concurrency
    max_mem = _positive_int_env(source, WORKER_MAX_MEMORY_ENV)
    if max_mem is not None:
        conf.worker_max_memory_per_child = max_mem
    max_tasks = _positive_int_env(source, WORKER_MAX_TASKS_ENV)
    if max_tasks is not None:
        conf.worker_max_tasks_per_child = max_tasks


def configure_worker_reliability(app: Any) -> None:
    """Framework delivery defaults of the worker's Celery app.

    - ``task_ignore_result=True``: a task stores no result — the framework
      never reads results, and each stored one is a backend write + TTL key;
    - ``worker_prefetch_multiplier=1``: a child reserves one message at a
      time, so a long task does not strand a prefetched backlog behind it (and
      a crashed child loses at most its own message's ack).

    ``acks_late`` stays OFF globally (Celery's at-most-once default: a task is
    acked when it starts). An IDEMPOTENT task opts in to at-least-once with
    ``@task(at_least_once=True)``: it is registered ``acks_late`` and
    ``reject_on_worker_lost``, acked only after it finished, and redelivered if
    the worker process dies mid-task — only safe when running it twice is
    harmless.
    """
    conf = app.conf
    conf.task_ignore_result = True
    conf.worker_prefetch_multiplier = 1


# ------------------------------------------------------------------- heartbeat


def heartbeat_path(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(HEARTBEAT_FILE_ENV) or "").strip() or DEFAULT_HEARTBEAT_FILE


def install_heartbeat(app: Any, path: str | None = None) -> None:
    """Touch a heartbeat file from the worker's own timer (liveness probe).

    The timer runs in the worker's main process event machinery, so the file
    goes stale when the worker is wedged or dead — not merely when a child is
    busy. ``sandloom worker check`` checks its age without booting the app (no
    broker round-trip, no second copy of the composition root inside the
    container's memory limit, unlike ``celery inspect ping``).
    """
    bootsteps = importlib.import_module("celery.bootsteps")

    if getattr(app, "_sandloom_heartbeat_installed", False):
        return  # one heartbeat step per app, however often it is assembled
    target = Path(path or heartbeat_path())

    step_base: Any = bootsteps.StartStopStep

    class _Heartbeat(step_base):  # type: ignore[misc]
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
    max_age: float,
    now: float | None = None,
) -> bool:
    """True when the heartbeat file exists and was touched within ``max_age``."""
    try:
        mtime = os.path.getmtime(path or heartbeat_path())
    except OSError:
        return False
    return ((time.time() if now is None else now) - mtime) <= max_age


# ------------------------------------------------------------------ task table


def _cron_fields(cron: str) -> dict[str, str]:
    minute, hour, day_of_month, month_of_year, day_of_week = cron.split()
    return {
        "minute": minute,
        "hour": hour,
        "day_of_month": day_of_month,
        "month_of_year": month_of_year,
        "day_of_week": day_of_week,
    }


def _celery_task_for(name: str, run: Callable[..., None]) -> Any:
    """The Celery task body of the declared task ``name``: its message's
    keyword arguments are the payload object, which ``run`` (the boot's
    ``run``) turns back into the payload model. Bound, for the message's id."""

    def body(self: Any, **payload: Any) -> None:
        run(name, orjson.dumps(payload), task_id=str(self.request.id or ""))

    body.__name__ = name.rsplit(".", 1)[-1]
    return body


def register_task_table(app: Any, table: TaskTable, run: Callable[..., None]) -> None:
    """Register ``table`` on the Celery ``app``: one task per entry (queue
    routing, ``acks_late`` and ``reject_on_worker_lost`` for ``at_least_once``)
    and the beat schedule of the periodic entries. A message runs
    ``run(name, payload, task_id=...)`` (``WorkerBoot.run``)."""
    routes: dict[str, dict[str, str]] = dict(app.conf.task_routes or {})
    for name, spec in table.tasks.items():
        app.task(
            name=name,
            bind=True,
            # Only this app's: a shared task joins every app finalized later.
            shared=False,
            acks_late=spec.at_least_once,
            reject_on_worker_lost=spec.at_least_once,
            ignore_result=True,
        )(_celery_task_for(name, run))
        if spec.queue != "default":
            routes[name] = {"queue": spec.queue}
    if routes:
        app.conf.task_routes = routes
    periodic = tuple(table.periodic)
    if periodic:
        app.on_after_finalize.connect(
            functools.partial(_add_periodic_tasks, periodic=periodic), weak=False
        )


def _add_periodic_tasks(
    sender: Any, *, periodic: Sequence[PeriodicSpec], **_: Any
) -> None:
    # Celery loads only where the beat is assembled (a module-level import would not).
    crontab = importlib.import_module("celery.schedules").crontab

    for spec in periodic:
        sender.add_periodic_task(
            crontab(**_cron_fields(spec.cron)),
            sender.signature(spec.task),
            name=spec.id,
        )


# ------------------------------------------------------------------ beat lease


class BeatLease:
    """The scheduler's lease: a synchronous facade over the kernel's
    :class:`~sandloom.driverkit.FencedLease` (held, not inherited).

    Beat's scheduler loop is synchronous; the lease's store operations run on
    the runner's loop (the boot's ``run_coroutine``: the persistent loop of the
    process, where the store is also disposed).
    """

    def __init__(
        self,
        store: LeaseStore,
        name: str,
        *,
        ttl_seconds: int,
        runner: Callable[[Coroutine[Any, Any, Any]], Any],
        clock: Callable[[], float] = time.monotonic,
        identity: str | None = None,
    ) -> None:
        self._holder = FencedLease(
            store,
            name,
            ttl_seconds=ttl_seconds,
            clock=clock,
            identity=identity,
            label="[beat]",
        )
        self._run = runner

    @property
    def name(self) -> str:
        return self._holder.name

    @property
    def held(self) -> bool:
        return self._holder.held

    @property
    def token(self) -> str | None:
        return self._holder.token

    @property
    def fence(self) -> int | None:
        return self._holder.fence

    @property
    def renew_interval(self) -> float:
        return self._holder.renew_interval

    def next_check_in(self) -> float:
        """Seconds until :meth:`ensure` has work to do (renew or retry)."""
        return self._holder.next_check_in()

    def ensure(self) -> bool:
        """Acquire or renew as due; ``True`` while this process may schedule."""
        fast = self._holder.fast_path()
        if fast is not None:
            return fast
        return bool(self._run(self._holder.ensure_async()))

    def open(self) -> None:
        """Open the store, so a backend that refuses to serve fails now, with
        its own message, instead of leaving beat on standby for good. A store
        that is only unreachable is retried by the holder."""
        try:
            opener = getattr(self._holder.store, "aopen", None)
            if opener is not None:
                self._run(opener())
        except (Unavailable, Timeout) as exc:
            logger.warning(
                "[beat] lease backend not reachable yet (%s); retrying",
                type(exc).__name__,
            )

    def release(self) -> None:
        """Release our own lease (best-effort) so a standby takes over at once."""
        if self.token is not None:
            self._run(self._holder.release_async())


class LeasedScheduler:
    """Wrap a Celery beat scheduler: it ticks only while the lease is held."""

    def __init__(self, inner: Any, lease: BeatLease) -> None:
        self._inner = inner
        self.lease = lease

    def tick(self, *args: Any, **kwargs: Any) -> float:
        if not self.lease.ensure():
            return self.lease.next_check_in()
        interval = self._inner.tick(*args, **kwargs)
        if interval and interval > 0:
            return min(float(interval), self.lease.next_check_in())
        return float(interval or 0)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self.lease.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install_beat_lease(app: Any, lease: BeatLease) -> Callable[..., None]:
    """Wrap every beat scheduler of ``app`` in :class:`LeasedScheduler`.

    Connected to ``beat_init`` (sent by ``beat.Service.start`` before the first
    tick). Returns the receiver, for :func:`remove_beat_lease`.
    """
    signals = _signals()

    def _on_beat_init(sender: Any = None, **_: Any) -> None:
        if sender is None or getattr(sender, "app", None) is not app:
            return
        scheduler = sender.scheduler
        if isinstance(scheduler, LeasedScheduler):
            return
        sender.scheduler = LeasedScheduler(scheduler, lease)
        logger.info("[beat] singleton lease %s installed", lease.name)

    signals.beat_init.connect(_on_beat_init, weak=False)
    return _on_beat_init


def remove_beat_lease(receiver: Callable[..., None]) -> None:
    _signals().beat_init.disconnect(receiver)


# ------------------------------------------------------------------------ host


def _loglevel(requested: str) -> str:
    level = (requested or "INFO").strip().upper()
    return (
        level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"
    )


def _run_celery(app: Any, argv: Sequence[str]) -> int:
    """Run one Celery command (``["worker", ...]``, ``["beat", ...]``) bound to
    ``app`` and return its exit code.

    ``Celery.start()`` calls the same CLI but discards its return value, so a
    worker that died would exit 0 and its container would never restart.
    """
    celery_cli = importlib.import_module("celery.bin.celery").celery
    celery_cli.params[0].default = app
    try:
        # standalone_mode=False: click returns the exit code (``ctx.exit(code)``)
        # instead of calling sys.exit.
        code = celery_cli.main(args=list(argv), standalone_mode=False)
    finally:
        celery_cli.params[0].default = None
    return code if isinstance(code, int) else 0


class CeleryWorkerHost:
    """The worker host of one Celery app (``CeleryTaskQueueProvider.worker_host``)."""

    def __init__(self, app_factory: Callable[[], Any]) -> None:
        self._app_factory = app_factory

    @property
    def _app(self) -> Any:
        """The Celery app, resolved when a command needs it: ``check`` never
        does, so the container probe imports no Celery."""
        return self._app_factory()

    def run_worker(
        self,
        boot: WorkerBoot,
        *,
        queues: Sequence[str] | None = None,
        concurrency: int | None = None,
        loglevel: str = "INFO",
    ) -> int:
        signals = _signals()
        app = self._app
        register_task_table(app, boot.boot(), boot.run)
        configure_worker_sizing(app, concurrency=concurrency)
        configure_worker_reliability(app)
        install_heartbeat(app)

        def on_worker_init(**_: Any) -> None:
            boot.before_fork()

        def on_process_init(**_: Any) -> None:
            boot.on_process_start()

        def on_process_shutdown(**_: Any) -> None:
            boot.on_process_stop()

        connected = (
            (signals.worker_init, on_worker_init),
            (signals.worker_process_init, on_process_init),
            (signals.worker_process_shutdown, on_process_shutdown),
        )
        for signal, receiver in connected:
            signal.connect(receiver, weak=False)
        argv = ["worker", f"--loglevel={_loglevel(loglevel)}"]
        if queues:
            argv.append(f"--queues={','.join(queues)}")
        if concurrency is not None:
            argv.append(f"--concurrency={concurrency}")
        try:
            return _run_celery(app, argv)
        finally:
            for signal, receiver in connected:
                signal.disconnect(receiver)

    def run_scheduler(
        self,
        boot: WorkerBoot,
        *,
        lease: LeaseStore,
        lease_name: str,
        lease_ttl_seconds: int = 30,
        loglevel: str = "INFO",
        state_file: str | None = None,
    ) -> int:
        app = self._app
        register_task_table(app, boot.boot(), boot.run)
        configure_worker_reliability(app)
        beat_lease = BeatLease(
            lease, lease_name, ttl_seconds=lease_ttl_seconds, runner=boot.run_coroutine
        )
        try:
            beat_lease.open()
        except InfraError as exc:
            # A backend that refuses to serve (not merely unreachable) would
            # leave the scheduler on standby for good: stop before the first tick.
            logger.critical("[beat] refusing to schedule: %s", exc)
            return 1
        receiver = install_beat_lease(app, beat_lease)
        argv = ["beat", f"--loglevel={_loglevel(loglevel)}"]
        if state_file:
            argv.append(f"--schedule={state_file}")
        try:
            return _run_celery(app, argv)
        finally:
            remove_beat_lease(receiver)
            try:
                boot.on_process_stop()
            except Exception:  # best-effort teardown of the process's pools
                logger.warning("[beat] process teardown failed", exc_info=True)

    def check(self, *, timeout_seconds: float) -> HealthResult:
        """``ok`` when the worker's heartbeat file was touched within
        ``timeout_seconds``. Boots nothing and asks no broker; the details
        name the heartbeat file, never the broker."""
        path = heartbeat_path()
        if heartbeat_is_fresh(path, max_age=timeout_seconds):
            return HealthResult(status="ok", details={"heartbeat": path})
        return HealthResult(
            status="fail",
            details={"error": f"worker heartbeat {path} missing or stale"},
        )


__all__ = [
    "BeatLease",
    "CeleryWorkerHost",
    "LeasedScheduler",
    "build_named_app",
    "configure_worker_reliability",
    "configure_worker_sizing",
    "default_app_matches",
    "get_default_app",
    "heartbeat_is_fresh",
    "heartbeat_path",
    "install_beat_lease",
    "install_heartbeat",
    "register_task_table",
    "remove_beat_lease",
]
