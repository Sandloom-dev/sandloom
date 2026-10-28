"""Gunicorn configuration for Sandloom production images.

Used as ``gunicorn -c python:sandloom.core.cli.gunicorn_conf sandloom.main:app`` by the
framework image and by the Dockerfile ``sandloom new`` generates.

Two concerns live here because they need the process model, not the app:

1. **Worker count.** ``nproc`` / ``os.cpu_count()`` report the HOST's CPUs and
   ignore the container's CFS quota (``deploy.resources.limits.cpus``), so a
   1-CPU container on a 16-core host used to start 16 workers — each with its
   own DB pool (16 x (DB_POOL_SIZE + DB_MAX_OVERFLOW) connections per service,
   far past Postgres ``max_connections``) and its own copy of the app (OOM at
   512m). The effective CPU count is read from the cgroup quota (v2
   ``cpu.max``, v1 ``cpu.cfs_quota_us``/``cpu.cfs_period_us``), falling back to
   the scheduler affinity / ``os.cpu_count()``. Default workers =
   ``min(2 * cpus + 1, 4)``; ``WEB_CONCURRENCY`` (when set and non-empty) wins.

2. **One outbox relay per process group.** The relay runs inside the app
   lifespan (``OUTBOX_RELAY_ENABLED``: ``auto`` = prod). Under gunicorn every
   worker would start its own relay loop; the relay is concurrency-safe
   (``FOR UPDATE SKIP LOCKED``) but N loops multiply polling load and
   connections. The master marks exactly ONE live worker as the relay leader
   and sets ``OUTBOX_RELAY_ENABLED=delegated`` in every other worker before the app
   is imported. Leadership survives worker churn: a dead leader's replacement
   is elected, a SIGHUP reload's new generation elects its own leader, and a
   leader retired without a replacement (TTOU) is re-elected by recycling one
   worker (see the hooks below).

Worker class and keep-alive:

* ``uvicorn_worker.UvicornWorker`` (the maintained home of the class formerly
  at ``uvicorn.workers``) with ``loop="auto"`` / ``http="auto"``: uvloop and
  httptools (``uvicorn[standard]``) are used whenever importable, falling back
  to asyncio / h11. Each worker logs the loop and HTTP implementation it
  booted with (``post_worker_init``).
* ``keepalive`` (``GUNICORN_KEEPALIVE``, default 75 s) becomes uvicorn's
  ``timeout_keep_alive``. It must EXCEED the idle timeout of whatever sits in
  front (AWS ALB 60 s, nginx ``keepalive_timeout`` 60-75 s, GCP LB 600 s —
  raise it there): if the app closes an idle upstream connection first, the
  balancer can reuse a socket the app is already closing and the client gets
  a sporadic 502. uvicorn's own default (5 s) is far below every LB default.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import logging
import math
import os
import shutil
import signal
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from sandloom.core.cpu_quota import available_cpus
from sandloom.core.gc_tuning import GcAfterStartup, GcConfig

MAX_DEFAULT_WORKERS = 4
#: Default idle keep-alive (s): above the common LB idle timeouts (ALB 60 s).
DEFAULT_KEEPALIVE_SECONDS = 75


def default_workers(cpus: float | None = None) -> int:
    effective = available_cpus() if cpus is None else cpus
    return max(1, min(2 * math.ceil(effective) + 1, MAX_DEFAULT_WORKERS))


def resolve_workers(env: dict[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = (source.get("WEB_CONCURRENCY") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default_workers()


def _int_env(name: str, default: int, env: dict[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = (source.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_keepalive(env: dict[str, str] | None = None) -> int:
    """Idle keep-alive seconds: ``GUNICORN_KEEPALIVE`` or 75."""
    return _int_env("GUNICORN_KEEPALIVE", DEFAULT_KEEPALIVE_SECONDS, env)


def server_implementation(loop_setting: str = "auto") -> dict[str, str]:
    """The event loop and HTTP parser a worker runs with ("auto" resolved).

    uvicorn >= 0.36 no longer installs an event-loop *policy*: it hands a loop
    factory to ``asyncio.run``. So the running loop is authoritative; outside
    one (gunicorn's ``post_worker_init`` runs before the worker's loop starts)
    the configured ``loop`` setting is resolved the way uvicorn resolves it
    (``auto`` = uvloop when installed).
    """
    try:
        running: type | None = type(asyncio.get_running_loop())
    except RuntimeError:
        running = None
    if running is not None:
        uv = running.__module__.startswith("uvloop")
    elif loop_setting == "auto":
        uv = importlib.util.find_spec("uvloop") is not None
    else:
        uv = loop_setting == "uvloop"
    http = "httptools" if importlib.util.find_spec("httptools") else "h11"
    return {"loop": "uvloop" if uv else "asyncio", "http": http}


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:2222")
workers = resolve_workers()
worker_class = "uvicorn_worker.UvicornWorker"
# Passed to uvicorn as timeout_keep_alive; see the module docstring.
keepalive = resolve_keepalive()
# Access lines include query strings and bypass application log redaction in
# UvicornWorker. Keep them opt-in; HTTP RED metrics remain enabled.
accesslog = os.environ.get("GUNICORN_ACCESSLOG") or None
errorlog = "-"
# Recycle a worker that stops heart-beating instead of hanging the slot.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
_shutdown_budget = float(os.environ.get("SHUTDOWN_BUDGET_SECONDS", "30"))
if not math.isfinite(_shutdown_budget) or _shutdown_budget <= 0:
    raise ValueError("SHUTDOWN_BUDGET_SECONDS must be a positive finite number")
graceful_timeout = int(
    os.environ.get(
        "GUNICORN_GRACEFUL_TIMEOUT", str(max(45, math.ceil(_shutdown_budget) + 10))
    )
)
if graceful_timeout <= _shutdown_budget:
    raise ValueError(
        "GUNICORN_GRACEFUL_TIMEOUT must exceed SHUTDOWN_BUDGET_SECONDS "
        "to allow request drain before application shutdown"
    )


def on_starting(server: Any) -> None:
    """Own one clean Prometheus mmap directory for this master lifetime.

    Must precede application import: prometheus_client selects its storage at
    import time. Preloading would import before this hook and is refused.
    A lock prevents two masters sharing/clearing each other's counters.
    """
    if getattr(getattr(server, "cfg", None), "preload_app", False):
        raise RuntimeError("Sandloom multiprocess metrics require preload_app=False")

    configured = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if configured and os.environ.get("SANDLOOM_PROMETHEUS_AUTODIR") == configured:
        # A USR2 re-exec inherits the old master's environment. Its workers
        # need their own directory while the old generation still serves.
        configured = None
    path = (
        Path(configured)
        if configured
        else Path(tempfile.mkdtemp(prefix=f"sandloom-prometheus-{os.getpid()}-"))
    )
    path.mkdir(parents=True, exist_ok=True)
    lock = (path / ".sandloom-master.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(
            "PROMETHEUS_MULTIPROC_DIR is already owned by another Gunicorn master"
        ) from None
    for stale in path.glob("*.db"):
        stale.unlink()
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(path)
    server.sandloom_metrics_dir = str(path)
    server.sandloom_metrics_dir_temporary = not configured
    if not configured:
        os.environ["SANDLOOM_PROMETHEUS_AUTODIR"] = str(path)
    server.sandloom_metrics_lock = lock


def on_exit(server: Any) -> None:
    """Release only the directory created by this master, after workers exit."""
    lock = getattr(server, "sandloom_metrics_lock", None)
    if lock is None:
        return
    path = getattr(server, "sandloom_metrics_dir", "")
    if getattr(server, "sandloom_metrics_dir_temporary", False):
        shutil.rmtree(path, ignore_errors=True)
    lock.close()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR") == path:
        os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
    if os.environ.get("SANDLOOM_PROMETHEUS_AUTODIR") == path:
        os.environ.pop("SANDLOOM_PROMETHEUS_AUTODIR", None)


# Relay leadership. Exactly one worker per master runs the in-process outbox
# relay; the master elects it at fork time (``pre_fork``), the only moment a
# worker's environment can still be set. Leadership is scoped to a worker
# GENERATION: a SIGHUP graceful reload forks a whole new generation while the
# old one (and its leader) still runs, so the new generation elects its own
# leader (``on_reload`` starts it) — two relays overlap until the old leader
# exits, which is safe: relays claim rows with SKIP LOCKED + lease tokens.
# A leader retired WITHOUT a replacement fork (TTOU retires the oldest worker,
# usually the leader) is re-elected by gracefully recycling one live worker:
# its replacement fork is elected in ``pre_fork`` (``child_exit``).


def _generation(server: Any) -> int:
    return int(getattr(server, "sandloom_relay_generation", 0))


def _current_generation(server: Any) -> list[tuple[int, Any]]:
    generation = _generation(server)
    workers = getattr(server, "WORKERS", {}) or {}
    return [
        (pid, w)
        for pid, w in workers.items()
        if getattr(w, "sandloom_relay_generation", 0) == generation
    ]


def on_reload(server: Any) -> None:
    """(master, SIGHUP) The next forks are a new generation: elect anew."""
    server.sandloom_relay_generation = _generation(server) + 1


def pre_fork(server: Any, worker: Any) -> None:
    """(master) Mark this worker relay leader iff its generation has none."""
    worker.sandloom_relay_generation = _generation(server)
    has_leader = any(
        getattr(w, "sandloom_relay_leader", False)
        for _, w in _current_generation(server)
    )
    worker.sandloom_relay_leader = not has_leader


def child_exit(server: Any, worker: Any) -> None:
    """(master) Re-elect when the exiting leader gets no replacement fork."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR") and getattr(worker, "pid", None):
        from prometheus_client.multiprocess import mark_process_dead  # noqa: PLC0415 - optional: multiprocess metrics only with PROMETHEUS_MULTIPROC_DIR; prometheus_client fixes its storage at import, so the master imports it only after on_starting

        cast(Callable[[int], None], mark_process_dead)(worker.pid)
    if not getattr(worker, "sandloom_relay_leader", False):
        return
    if getattr(worker, "sandloom_relay_generation", 0) != _generation(server):
        return  # an old generation's leader: the current one has its own
    alive = _current_generation(server)
    if any(getattr(w, "sandloom_relay_leader", False) for _, w in alive):
        return
    workers = getattr(server, "WORKERS", {}) or {}
    if len(workers) < int(getattr(server, "num_workers", 0) or 0):
        return  # the master forks a replacement, elected in pre_fork
    if not alive:
        return
    pid, _ = max(alive, key=lambda item: getattr(item[1], "age", 0))
    server.log.info(
        "sandloom: relay leader worker exited without a replacement; recycling "
        "worker %s so its replacement takes the outbox relay",
        pid,
    )
    server.kill_worker(pid, signal.SIGTERM)


def post_fork(server: Any, worker: Any) -> None:
    """(worker) Non-leaders never start the in-process outbox relay."""
    os.environ["SANDLOOM_WORKER_COUNT"] = str(
        max(1, int(getattr(server, "num_workers", workers)))
    )
    if not getattr(worker, "sandloom_relay_leader", False):
        os.environ["OUTBOX_RELAY_ENABLED"] = "delegated"


def _configure_gc(worker: Any) -> None:
    """Freeze / thresholds / metrics once the app's lifespan startup is done.

    ``post_worker_init`` runs after the app import but BEFORE the ASGI
    lifespan (it starts inside ``worker.run()``), so the GC configuration is
    applied by an ASGI wrapper on ``lifespan.startup.complete``: the frozen
    heap then includes pools, consumers and caches built at startup, and the
    worker accepts no connection before it is applied
    (``sandloom.core.gc_tuning``, ``GC_*`` settings).
    """
    app = getattr(worker, "wsgi", None)
    if app is None:
        return
    config = GcConfig.from_settings()
    if config.freeze_after_startup or config.thresholds or config.metrics:
        worker.wsgi = GcAfterStartup(app, config)


def post_worker_init(worker: Any) -> None:
    """(worker) Log the loop / HTTP implementation actually in use."""
    from sandloom.core.observability.logging import AccessLogFilter, JsonFormatter  # noqa: PLC0415 - cold: gunicorn loads this config in the master; the logging stack (and settings) load in the worker hook

    # UvicornWorker copies Gunicorn's handlers and disables propagation.
    # Explicitly enabled access logs still need the application's redaction
    # boundary; omit query values even if their parameter names are unknown.
    for logger_name in ("gunicorn.access", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(f, AccessLogFilter) for f in logger.filters):
            logger.addFilter(AccessLogFilter())
        for handler in logger.handlers:
            handler.setFormatter(JsonFormatter())
    _configure_gc(worker)
    uvicorn_config = getattr(worker, "config", None)
    impl = server_implementation(str(getattr(uvicorn_config, "loop", "auto")))
    worker.log.info(
        "sandloom worker booted: loop=%s http=%s keepalive=%ss",
        impl["loop"],
        impl["http"],
        getattr(getattr(worker, "cfg", None), "keepalive", keepalive),
    )
