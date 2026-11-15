"""The Celery worker host of the ``celery`` taskqueue driver (``sandloom-celery``).

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
  Celery app, which the provider (``sandloom_celery.taskqueue``) and this host
  share.

**The importable Celery app.** ``celery -A sandloom_celery.worker
inspect|call|status`` and Flower use the module attribute ``app`` (also
``celery`` and ``celery_app``): the ``default`` app, built lazily from
``BROKER_URL``, with no composition root and no task registered. It is the
driver's own interface, not a shim.

**Process options.** The port carries the log level and the scheduler's state
file (``state_file``: Celery beat's ``--schedule``, default ``celerybeat-schedule``
in the working directory); the Celery-specific sizing knobs
``WORKER_MAX_MEMORY_PER_CHILD`` and ``WORKER_MAX_TASKS_PER_CHILD`` and the
liveness file ``WORKER_HEARTBEAT_FILE`` are read here.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Literal, NoReturn

import orjson

from sandloom.driverkit import (
    FencedLease,
    HealthResult,
    InfraError,
    InvalidArgument,
    Lease,
    LeaseStore,
    PeriodicSpec,
    TaskTable,
    Timeout,
    TooLarge,
    Unavailable,
    WorkerBoot,
)
from sandloom.driverkit.periodic import (
    CronSchedule,
    PeriodicSlot,
    SlotClaim,
    check_slot_lease_name,
    claim_periodic_slot,
    periodic_slot,
    slot_lease_name,
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
#: The pools of the ``worker_pool`` option (the first is the default): ``prefork``
#: forks child processes, ``solo`` runs the messages in the worker process
#: itself (inside Celery's event loop), ``threads`` on a thread pool of it.
WorkerPool = Literal["prefork", "solo", "threads"]
#: The operator's worker concurrency (the kernel resolves it; ``solo`` runs one
#: message at a time, so an explicit value above 1 is refused there).
WORKER_CONCURRENCY_ENV = "WORKER_CONCURRENCY"
#: The broker of the importable app (the kernel's ``BROKER_URL`` setting).
BROKER_URL_ENV = "BROKER_URL"
#: Celery CLI global options whose click ``envvar`` (``APP``, ``BROKER_URL``,
#: ``RESULT_BACKEND``, ``LOADER``, ``CONFIG_MODULE``) must not reach a host run.
_CLI_ENV_OPTIONS = frozenset({"app", "broker", "result_backend", "loader", "config"})
#: Names ``app.conf`` / the loader read from ``os.environ`` on every use; the
#: host's app and options decide, so they are hidden for the run.
_CONF_ENV = (
    "CELERY_APP",
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
    "CELERY_LOADER",
    "CELERY_CONFIG_MODULE",
)

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
    # ``celery -A sandloom_celery.worker`` looks up ``app`` (or
    # ``celery``); building lazily keeps a plain import free of side effects.
    if name in {"app", "celery", "celery_app"}:
        if _default_app is not None:
            return _refuse_unhosted_runs(_default_app)
        broker = (os.environ.get(BROKER_URL_ENV) or "").strip()
        if not broker:
            raise AttributeError(
                f"the importable Celery app needs {BROKER_URL_ENV} (the broker "
                "of the default taskqueue instance)"
            )
        return _refuse_unhosted_runs(get_default_app(broker, None))
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _refuse(message: str) -> NoReturn:
    """Stop the process with exit code 1. ``SystemExit`` is not an ``Exception``,
    so Celery's signal dispatch lets it through."""
    logger.critical("%s", message)
    raise SystemExit(1)


_refusal_connected = False


def _refuse_unhosted_runs(app: Any) -> Any:
    """Mark ``app`` as the importable one and make ``celery -A
    sandloom_celery.worker worker|beat`` refuse to start.

    The importable app registers no task (the task table is built by
    ``sandloom worker`` / ``sandloom beat``): a worker on it would consume
    messages and drop them as unknown tasks, a beat would run without the
    lease. A host run (:meth:`CeleryWorkerHost.run_worker` /
    :meth:`CeleryWorkerHost.run_scheduler`) marks its app hosted and passes.
    """
    global _refusal_connected
    app._sandloom_importable = True
    if not _refusal_connected:
        signals = _signals()
        signals.worker_init.connect(_refuse_worker_init, weak=False)
        signals.beat_init.connect(_refuse_beat_init, weak=False)
        _refusal_connected = True
    return app


def _is_unhosted_importable(sender: Any) -> bool:
    app = getattr(sender, "app", None)
    return (
        app is not None
        and getattr(app, "_sandloom_importable", False)
        and not getattr(app, "_sandloom_hosted", False)
    )


def _refuse_worker_init(sender: Any = None, **_: Any) -> None:
    if _is_unhosted_importable(sender):
        _refuse(
            "the importable Celery app runs no tasks (the task table is built "
            "by the kernel): start the worker with `sandloom worker`"
        )


def _refuse_beat_init(sender: Any = None, **_: Any) -> None:
    if _is_unhosted_importable(sender):
        _refuse(
            "the importable Celery app has no beat lease: "
            "start the scheduler with `sandloom beat`"
        )


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


def refuse_unusable_pool(pool: WorkerPool, env: dict[str, str] | None = None) -> None:
    """Refuse, before anything boots, a ``worker_pool`` the process cannot run
    as configured (:class:`InvalidArgument`, naming the option or setting):

    - ``prefork`` where billiard does not start its children with ``fork``
      (macOS: it spawns, and a spawned child never sees the boot-registered
      task table): set ``worker_pool`` to ``solo`` or ``threads``;
    - ``solo`` / ``threads`` with ``WORKER_MAX_MEMORY_PER_CHILD`` or
      ``WORKER_MAX_TASKS_PER_CHILD`` set: they recycle prefork children, and
      those pools have none;
    - ``solo`` with ``WORKER_CONCURRENCY`` above 1: it runs one message at a time.
    """
    source = os.environ if env is None else env
    if pool == "prefork":
        method = importlib.import_module("billiard").get_start_method()
        if method != "fork":
            raise InvalidArgument(
                "worker_pool 'prefork' needs billiard to fork its children, and "
                f"here it starts them with {method!r} (macOS): set worker_pool to "
                "'solo' or 'threads' in the taskqueue instance's options (INFRA), "
                "or run the worker on Linux",
                capability="taskqueue",
            )
        return
    for key in (WORKER_MAX_MEMORY_ENV, WORKER_MAX_TASKS_ENV):
        if _positive_int_env(source, key) is not None:
            raise InvalidArgument(
                f"{key} recycles prefork children and worker_pool {pool!r} has "
                "none: unset it or set worker_pool to 'prefork'",
                capability="taskqueue",
            )
    if pool == "solo":
        concurrency = _positive_int_env(source, WORKER_CONCURRENCY_ENV)
        if concurrency is not None and concurrency > 1:
            raise InvalidArgument(
                f"{WORKER_CONCURRENCY_ENV}={concurrency} but worker_pool 'solo' "
                "runs one message at a time: unset it, or set worker_pool to "
                "'threads' or 'prefork'",
                capability="taskqueue",
            )


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


def _touch(target: Path) -> None:
    try:
        target.touch()
    except OSError:  # pragma: no cover - read-only fs
        logger.warning("[worker] cannot touch heartbeat %s", target)


class _HeartbeatThread:
    """A daemon thread that calls ``beat`` every ``interval`` seconds until
    stopped (the heartbeat of a pool that runs messages inside the timer's
    loop)."""

    def __init__(self, beat: Callable[[], None], interval: float) -> None:
        self._beat = beat
        self._interval = interval
        self._halt = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sandloom-heartbeat", daemon=True
        )

    def _run(self) -> None:
        while not self._halt.wait(self._interval):
            self._beat()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(timeout=self._interval + 1)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def install_heartbeat(
    app: Any,
    path: str | None = None,
    *,
    thread: bool = False,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Touch a heartbeat file from the worker's own timer (liveness probe).

    The timer runs in the worker's main process event machinery, so the file
    goes stale when the worker is wedged or dead — not merely when a child is
    busy. ``sandloom worker check`` checks its age without booting the app (no
    broker round-trip, no second copy of the composition root inside the
    container's memory limit, unlike ``celery inspect ping``).

    ``thread=True`` (the ``solo`` and ``threads`` pools) touches the file from a
    daemon thread instead: ``solo`` runs a message inside the same event loop
    as the timer, so a task longer than the check window would starve the timer
    and ``sandloom worker check`` would declare a busy worker dead. The price:
    the file no longer shows a loop wedged by something other than a task.
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
            self.ticker: _HeartbeatThread | None = None

        def _beat(self, *_: Any) -> None:
            if not self.stopped:
                _touch(target)

        def start(self, parent: Any) -> None:
            self._beat()
            if thread:
                self.ticker = _HeartbeatThread(self._beat, interval)
                self.ticker.start()
            else:
                self.tref = parent.timer.call_repeatedly(
                    interval, self._beat, (), priority=10
                )

        def stop(self, parent: Any) -> None:
            self.stopped = True
            if self.ticker is not None:
                self.ticker.stop()
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


# ------------------------------------------------------------------ cron slots
#
# The scheduler deduplicates a run by (periodic id, slot) (spec 8.2). The slot
# math and the claim of a slot are the driverkit's (sandloom.driverkit.periodic);
# this is the adapter from a Celery ``crontab``.

_ALL_DAYS_OF_MONTH = frozenset(range(1, 32))
_ALL_DAYS_OF_WEEK = frozenset(range(7))


def cron_schedule_of(schedule: Any) -> CronSchedule | None:
    """The :class:`CronSchedule` of a ``celery.schedules.crontab`` (the zone is
    ``schedule.tz``), or ``None`` for one it cannot name a slot of: a
    day-of-month and a day-of-week that are both restricted (Celery ands them,
    a ``CronSchedule`` reads one)."""
    days_of_month = frozenset(schedule.day_of_month)
    days_of_week = frozenset(schedule.day_of_week)
    day_of_month_any = days_of_month == _ALL_DAYS_OF_MONTH
    day_of_week_any = days_of_week == _ALL_DAYS_OF_WEEK
    if not day_of_month_any and not day_of_week_any:
        return None
    minutes = frozenset(schedule.minute)
    hours = frozenset(schedule.hour)
    months = frozenset(schedule.month_of_year)
    expression = " ".join(
        ",".join(str(value) for value in sorted(values))
        for values in (minutes, hours, days_of_month, months, days_of_week)
    )
    return CronSchedule(
        expression=expression,
        minutes=minutes,
        hours=hours,
        days_of_month=days_of_month,
        months=months,
        days_of_week=days_of_week,
        day_of_month_any=day_of_month_any,
        day_of_week_any=day_of_week_any,
    )


_NOT_SENT_REASON = {
    "taken": "another scheduler sent it",
    "not_held": "the lease is not held",
    "refused": "the lease store refused the claim",
}


def _nothing_published(exc: BaseException) -> bool:
    """Is ``exc`` (or its cause) a refusal before anything reached the broker
    (``InvalidArgument``, ``TooLarge``)? Any other failure of a send, a broker
    timeout included, may have been published (spec 8.2): the claim stays."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, InvalidArgument | TooLarge):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


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
        self._identity = identity or f"{socket.gethostname()}:{os.getpid()}"
        self._holder = FencedLease(
            store,
            name,
            ttl_seconds=ttl_seconds,
            clock=clock,
            identity=self._identity,
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

    def claim_slot(
        self,
        periodic_id: str,
        schedule: CronSchedule,
        zone: tzinfo,
        slot: PeriodicSlot,
    ) -> SlotClaim:
        """Claim the run of ``periodic_id`` for ``slot`` (spec 8.2: a run is
        deduplicated by ``(id, slot)`` under the lease's fence).

        The claim is one lease name per periodic id
        (:func:`~sandloom.driverkit.periodic.slot_lease_name`), granted for the
        slot's lifetime: a second scheduler that held the scheduler lease for
        the same slot (after a takeover, or after a send that blocked past the
        lease TTL) is refused. Only a holder claims (``not_held`` otherwise: it
        must not send). A store that does not answer lets the slot through
        (``unknown``: ``periodic`` promises at least one run, a run may
        repeat); any other refusal sends nothing (``refused``).
        """
        if not self.ensure():
            return SlotClaim("not_held")
        name = slot_lease_name(self.name, periodic_id, schedule, zone)
        return self._run(  # type: ignore[no-any-return]
            claim_periodic_slot(
                self._holder.store,
                name,
                slot,
                holder=self._identity,
                fence=self.fence,
                limit_seconds=self.renew_interval,
            )
        )

    def release_slot(self, claim: SlotClaim) -> None:
        """Give a claimed slot back (best-effort): nothing was published, so
        another scheduler may send it."""
        if claim.lease is None:
            return
        lease = claim.lease
        try:
            self._run(self._release_slot_async(lease))
        except (InfraError, OSError):  # the claim expires with its slot anyway
            logger.warning("[beat] could not release the claim of slot %s", lease.name)

    async def _release_slot_async(self, lease: Lease) -> None:
        await asyncio.wait_for(
            self._holder.store.release(lease), timeout=self.renew_interval
        )


class LeasedScheduler:
    """Wrap a Celery beat scheduler: it ticks only while the lease is held, and
    sends each cron slot of a periodic task once (``(id, slot)`` dedupe under
    the lease, :meth:`BeatLease.claim_slot`).

    ``wall_clock`` is the current moment (aware); tests pass a fake one.
    """

    def __init__(
        self,
        inner: Any,
        lease: BeatLease,
        *,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._inner = inner
        self.lease = lease
        self._wall_clock = wall_clock
        send = getattr(inner, "apply_async", None)
        if send is not None:
            # Celery's beat sends a due entry through ``apply_async`` of its own
            # scheduler: the one place every send passes.
            self._send = send
            inner.apply_async = self._apply_async

    def _apply_async(
        self, entry: Any, producer: Any = None, advance: bool = True, **kwargs: Any
    ) -> Any:
        crontab = importlib.import_module("celery.schedules").crontab
        schedule: Any = getattr(entry, "schedule", None)
        cron = cron_schedule_of(schedule) if isinstance(schedule, crontab) else None
        slot = None
        if cron is not None:
            moment = self._wall_clock() if self._wall_clock else datetime.now(UTC)
            slot = periodic_slot(cron, schedule.tz, moment)
        if cron is None or slot is None:  # nothing to name a slot by
            return self._send(entry, producer, advance, **kwargs)
        claim = self.lease.claim_slot(entry.name, cron, schedule.tz, slot)
        if not claim.may_send:
            if advance:
                self._inner.reserve(entry)
            logger.warning(
                "[beat] %s slot %s not sent: %s",
                entry.name,
                slot.key,
                _NOT_SENT_REASON.get(claim.state, claim.state),
            )
            return None
        try:
            return self._send(entry, producer, advance, **kwargs)
        except Exception as exc:
            # A refusal before the broker saw anything frees the slot; any
            # other failure may have been published, so the claim stays.
            if _nothing_published(exc):
                self.lease.release_slot(claim)
            raise

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
    # Bound here: the identity check below narrows ``app`` inside the receiver.
    beat_clock = app.now

    def _on_beat_init(sender: Any = None, **_: Any) -> None:
        if sender is None or getattr(sender, "app", None) is not app:
            # A scheduler on another app would tick WITHOUT the lease: never run.
            _refuse(
                "the beat scheduler is not the host's app, so it would run "
                f"without the singleton lease {lease.name}: refusing to schedule"
            )
        scheduler = sender.scheduler
        if isinstance(scheduler, LeasedScheduler):
            return
        # The slot of a send is read on the clock the beat itself ticks on
        # (``app.now``), never a second one: with two clocks a tick due in one
        # minute could claim the slot of the next.
        sender.scheduler = LeasedScheduler(scheduler, lease, wall_clock=beat_clock)
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


def _process_hooks(
    signals: Any, boot: WorkerBoot, *, forks: bool
) -> list[tuple[Any, Callable[..., None]]]:
    """The ``(signal, receiver)`` pairs that call the boot's process hooks.

    ``prefork`` children announce themselves (``worker_process_init`` /
    ``worker_process_shutdown``). ``solo`` and ``threads`` run the messages in
    the worker process and send neither, so the host starts that process on
    ``worker_init`` and stops it on ``worker_shutdown``; no process is forked
    there, so ``before_fork`` (the kernel's GC freeze that children share) is
    not called."""

    def on_worker_init(**_: Any) -> None:
        if forks:
            boot.before_fork()
        else:
            boot.on_process_start()

    def on_process_init(**_: Any) -> None:
        boot.on_process_start()

    def on_process_shutdown(**_: Any) -> None:
        boot.on_process_stop()

    connected: list[tuple[Any, Callable[..., None]]] = [
        (signals.worker_init, on_worker_init),
        (signals.worker_process_init, on_process_init),
        (signals.worker_process_shutdown, on_process_shutdown),
    ]
    if not forks:
        connected.append((signals.worker_shutdown, on_process_shutdown))
    return connected


def _run_celery(app: Any, argv: Sequence[str]) -> int:
    """Run one Celery command (``["worker", ...]``, ``["beat", ...]``) bound to
    ``app`` and return its exit code.

    ``Celery.start()`` calls the same CLI but discards its return value, so a
    worker that died would exit 0 and its container would never restart.
    """
    celery_cli = importlib.import_module("celery.bin.celery").celery
    # The environment must not choose the app, the broker or the backend of a
    # process whose lease name and task table come from the host's app.
    # Celery reads it two ways: the CLI's global options (``APP``, ``BROKER_URL``,
    # ``RESULT_BACKEND``, ``LOADER``, ``CONFIG_MODULE``, as click ``envvar``) and
    # ``app.conf`` itself (``CELERY_BROKER_URL`` and the other ``CELERY_*``
    # names, on every read). The first is switched off on the command, the
    # second removed from ``os.environ`` for the run: Celery reads that mapping
    # directly, so a copy would not be seen.
    cli_options = [p for p in celery_cli.params if p.name in _CLI_ENV_OPTIONS]
    saved_envvars = {p.name: p.envvar for p in cli_options}
    saved_environ = {k: os.environ.pop(k) for k in _CONF_ENV if k in os.environ}
    args = list(argv)
    # The host's own broker and backend, passed explicitly (they win over every
    # default; the CLI mirrors them into ``CELERY_BROKER_URL`` for the run).
    broker = app.conf.broker_url
    if broker:
        args = ["--broker", str(broker), *args]
    backend = app.conf.result_backend
    if backend:
        args = ["--result-backend", str(backend), *args]
    for p in cli_options:
        p.envvar = None
    celery_cli.params[0].default = app
    try:
        # standalone_mode=False: click returns the exit code (``ctx.exit(code)``)
        # instead of calling sys.exit.
        code = celery_cli.main(args=args, standalone_mode=False)
    finally:
        celery_cli.params[0].default = None
        for p in cli_options:
            p.envvar = saved_envvars[p.name]
        for key in _CONF_ENV:
            os.environ.pop(key, None)
        os.environ.update(saved_environ)
    return code if isinstance(code, int) else 0


class CeleryWorkerHost:
    """The worker host of one Celery app (``CeleryTaskQueueProvider.worker_host``)."""

    def __init__(
        self, app_factory: Callable[[], Any], *, worker_pool: WorkerPool = "prefork"
    ) -> None:
        self._app_factory = app_factory
        self._worker_pool: WorkerPool = worker_pool

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
        pool = self._worker_pool
        # Refused before the kernel boots anything.
        refuse_unusable_pool(pool)
        signals = _signals()
        app = self._app
        app._sandloom_hosted = True
        register_task_table(app, boot.boot(), boot.run)
        # A solo pool runs one message at a time: Celery ignores a larger value.
        sized = None if pool == "solo" else concurrency
        configure_worker_sizing(app, concurrency=sized)
        configure_worker_reliability(app)
        install_heartbeat(app, thread=pool != "prefork")

        connected = _process_hooks(signals, boot, forks=pool == "prefork")
        for signal, receiver in connected:
            signal.connect(receiver, weak=False)
        argv = ["worker", f"--loglevel={_loglevel(loglevel)}", f"--pool={pool}"]
        if queues:
            argv.append(f"--queues={','.join(queues)}")
        if sized is not None:
            argv.append(f"--concurrency={sized}")
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
        try:
            check_slot_lease_name(lease_name)
        except ValueError as exc:
            # A name no store accepts for a slot would switch the dedupe off.
            logger.critical("[beat] refusing to schedule: %s", exc)
            return 1
        app = self._app
        app._sandloom_hosted = True
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
    "WorkerPool",
    "build_named_app",
    "configure_worker_reliability",
    "configure_worker_sizing",
    "default_app_matches",
    "get_default_app",
    "heartbeat_is_fresh",
    "heartbeat_path",
    "install_beat_lease",
    "install_heartbeat",
    "refuse_unusable_pool",
    "register_task_table",
    "remove_beat_lease",
]
