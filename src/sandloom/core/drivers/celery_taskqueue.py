"""Built-in Celery task-queue driver.

This driver is the SOLE constructor of Celery apps, including the process-wide
``default`` one (:func:`get_default_app`). Named instances build their own
Celery app from ``options['broker']`` / ``options['backend']``. The kernel only
depends on the abstract :class:`TaskQueueProvider` — modules never import Celery
directly. The framework worker (``sandloom.core.worker``) consumes the SAME app,
resolved through the registry, so enqueue and execution share one namespace.

The driver reads no kernel setting (the kernel fills ``broker`` and
``backend`` from ``BROKER_URL`` / ``CACHE_URL``) and only publishes: enqueue
dedupe (``idempotency_key``) is the kernel's taskqueue layer
(:mod:`sandloom.core.tasks.queue`), which reserves a job id and hands it to
:meth:`CeleryTaskQueueProvider.send` as ``task_id``. A failure that provably
published nothing is :class:`InvalidArgument`; anything else may have reached
the broker and is :class:`Unavailable` or :class:`Timeout` (spec 8.2).
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Self, cast
from urllib.parse import urlsplit

import orjson
from pydantic import BaseModel, ConfigDict, Field

from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.celery_broker_probe import probe_broker
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import (
    PeriodicSpec,
    TaskQueueProvider,
    TaskTable,
)
from sandloom.core.infra.errors import InvalidArgument, Timeout, Unavailable

#: Features the Celery task-queue driver supports. ``periodic``: the worker
#: registers each periodic task of the table and one beat per broker and module
#: set runs it under a fenced lease (sandloom.core.worker, beat_lease).
#: ``redelivery``: an ``at_least_once`` task is registered ``acks_late`` and
#: ``reject_on_worker_lost``.
CELERY_TASKQUEUE_FEATURES = frozenset({"delay", "periodic", "redelivery"})


class CeleryTaskQueueOptions(BaseModel):
    """Config-shape schema for the ``celery`` task-queue driver.

    The driver reads ``broker`` (message broker URL) and ``backend`` (result
    backend URL); the kernel fills both from ``BROKER_URL`` / ``CACHE_URL``
    when the configuration leaves them out. ``dedupe_ttl_seconds`` is the
    replay window of the kernel's enqueue dedupe for this instance.
    ``extra="forbid"`` rejects a typo'd key at startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    broker: str | None = None
    backend: str | None = None
    #: The kernel's record TTL (``RECORD_TTL_SECONDS``, one day), written out:
    #: the driver imports no kernel module (test_replay_limits keeps them equal).
    dedupe_ttl_seconds: int = Field(default=24 * 60 * 60, ge=1, le=604800)


#: Process-wide ``default`` Celery app, built lazily from the ``default``
#: instance's options, and the ``(broker, backend)`` it was built from.
_default_app: Any | None = None
_default_app_urls: tuple[str, str | None] | None = None
#: Threads that run the blocking broker publish of ``send``.
ENQUEUE_EXECUTOR_THREADS = 4
_executor: ThreadPoolExecutor | None = None
_executor_pid: int | None = None


def _enqueue_executor() -> ThreadPoolExecutor:
    """The process's enqueue executor (rebuilt in a forked child: threads do
    not survive ``fork``)."""
    global _executor, _executor_pid
    pid = os.getpid()
    if _executor is None or _executor_pid != pid:
        _executor = ThreadPoolExecutor(
            max_workers=ENQUEUE_EXECUTOR_THREADS, thread_name_prefix="sandloom-enqueue"
        )
        _executor_pid = pid
    return _executor


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
        from celery import Celery  # local import: optional heavy dependency  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        _default_app = _apply_framework_defaults(
            Celery("project", broker=broker, backend=backend)
        )
        _default_app_urls = (broker, backend)
    return _default_app


def _cron_fields(cron: str) -> dict[str, str]:
    minute, hour, day_of_month, month_of_year, day_of_week = cron.split()
    return {
        "minute": minute,
        "hour": hour,
        "day_of_month": day_of_month,
        "month_of_year": month_of_year,
        "day_of_week": day_of_week,
    }


def _celery_task_for(name: str, run: Callable[[str, bytes], None]) -> Any:
    """The Celery task body of the declared task ``name``: its message's
    keyword arguments are the payload object, which ``run`` (the kernel's
    ``run_task``) turns back into the payload model."""

    def body(**payload: Any) -> None:
        run(name, orjson.dumps(payload))

    body.__name__ = name.rsplit(".", 1)[-1]
    return body


def register_task_table(
    app: Any, table: TaskTable, run: Callable[[str, bytes], None]
) -> None:
    """Register ``table`` on the Celery ``app``: one task per entry (queue
    routing, ``acks_late`` and ``reject_on_worker_lost`` for ``at_least_once``)
    and the beat schedule of the periodic entries. A message runs ``run(name,
    payload)``. ``idk-worker-port`` moves this behind the driver's host."""
    routes: dict[str, dict[str, str]] = dict(app.conf.task_routes or {})
    for name, spec in table.tasks.items():
        app.task(
            name=name,
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


class CeleryTaskQueueProvider(TaskQueueProvider):
    """A single Celery application instance."""

    contract_version = 2
    supported_features = CELERY_TASKQUEUE_FEATURES
    options_schema = CeleryTaskQueueOptions
    required_imports = ("celery",)
    install_hint = "pip install 'sandloom[celery]'"

    @classmethod
    def from_config(cls, config: InstanceConfig) -> Self:
        return cast("Self", create_celery_taskqueue(config))

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        app: Any,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._app = app

    async def send(
        self,
        task: str,
        payload: bytes,
        *,
        task_id: str,
        delay: timedelta | None = None,
        at_least_once: bool = False,
        queue: str = "default",
    ) -> str:
        """Publish ``task`` once under ``task_id`` (the broker's job id; the
        kernel's taskqueue layer chose it).

        ``payload`` is the JSON object of the task's payload: it becomes the
        message's keyword arguments, which the worker's task of the same name
        turns back into the payload model. ``at_least_once`` is the worker's
        side (``acks_late`` and ``reject_on_worker_lost`` on the registered
        task); a message carries nothing for it. A failure that provably
        published nothing (the payload is not a JSON object or cannot be
        encoded, the task is unknown) is :class:`InvalidArgument`; anything
        else (connection drop, timeout, lost ack) may have reached the broker
        and is :class:`Unavailable` or :class:`Timeout`.
        """
        if not isinstance(task, str) or not task:
            raise InvalidArgument(
                "the task name must be a non-empty string",
                capability="taskqueue",
                instance=self.name,
            )
        try:
            kwargs = orjson.loads(payload)
        except orjson.JSONDecodeError as exc:
            raise InvalidArgument(
                "the task payload is not JSON",
                capability="taskqueue",
                instance=self.name,
            ) from exc
        if not isinstance(kwargs, dict):
            raise InvalidArgument(
                "the task payload must be a JSON object",
                capability="taskqueue",
                instance=self.name,
            )
        countdown: float | None = None
        if delay is not None:
            countdown = delay.total_seconds()
            if countdown < 0:
                raise InvalidArgument(
                    "delay must not be negative",
                    capability="taskqueue",
                    instance=self.name,
                )
        return await self._send(task, kwargs, countdown, task_id, queue)

    async def _send(
        self,
        task: str,
        payload: dict[str, Any],
        countdown: float | None,
        task_id: str,
        queue: str = "default",
    ) -> str:
        # send_task is a synchronous broker round-trip; offload it so the event
        # loop is never blocked on network I/O — on a small dedicated executor
        # (no per-call context copy, never starved by other to_thread users).
        # ``ignore_result=True``: enqueue returns only the id, so the producer
        # must not subscribe a result consumer per task (a Redis result backend
        # does that otherwise). Whether the WORKER stores a result is the
        # task's own ``ignore_result`` (``task_ignore_result`` default: True).
        send = functools.partial(
            self._app.send_task,
            task,
            kwargs=payload,
            countdown=countdown,
            task_id=task_id,
            ignore_result=True,
            retry=False,
            # The declared queue travels with the message: the producer need
            # not have registered the task table (an API process) to route it.
            **({} if queue == "default" else {"queue": queue}),
        )
        result = await asyncio.get_running_loop().run_in_executor(
            _enqueue_executor(), functools.partial(self._send_classified, send, payload)
        )
        return str(getattr(result, "id", result))

    def _send_classified(self, send: Any, kwargs: dict[str, Any]) -> Any:
        """``send`` with provably pre-broker failures raised as InvalidArgument.

        The kwargs are encoded with the app's task serializer first (what
        kombu does before any network write); kombu's own encode errors and
        an unknown task from ``send_task`` are pre-broker too. Everything else
        (connection drop, timeout, lost ack, any I/O error) may have reached
        the broker and is raised as :class:`Unavailable` / :class:`Timeout`,
        the original as its cause — the kernel keeps the dedupe key pending.
        """
        from celery.exceptions import NotRegistered  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver
        from kombu.exceptions import (  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver
            ContentDisallowed,
            EncodeError,
        )
        from kombu.serialization import dumps  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        serializer = str(
            getattr(getattr(self._app, "conf", None), "task_serializer", None) or "json"
        )
        try:
            dumps(kwargs, serializer=serializer)
        except Exception as exc:  # nothing was sent
            raise InvalidArgument(
                f"task kwargs cannot be encoded ({type(exc).__name__})",
                capability="taskqueue",
                instance=self.name,
            ) from exc
        try:
            return send()
        except (EncodeError, ContentDisallowed, NotRegistered) as exc:
            raise InvalidArgument(
                f"task not published ({type(exc).__name__})",
                capability="taskqueue",
                instance=self.name,
            ) from exc
        except Exception as exc:  # may have published: mapped, never swallowed
            raise _maybe_published(exc, self.name) from exc

    async def isolation_identity(self) -> str | None:
        """The broker this instance publishes to: scheme, host, port and path
        of the ``broker`` option, never its credentials (a rotation must not
        change the identity)."""
        broker = self.options.get("broker")
        if not isinstance(broker, str) or not broker:
            return None
        where = urlsplit(broker)
        port = f":{where.port}" if where.port else ""
        host = where.hostname or ""
        return f"{self.driver}:{where.scheme}://{host}{port}{where.path}"

    def register_task_table(
        self, table: TaskTable, run: Callable[[str, bytes], None]
    ) -> None:
        """Register ``table`` on this instance's Celery app (the worker boot):
        one task per entry (queue routing, ``acks_late`` and
        ``reject_on_worker_lost`` for ``at_least_once``) and the beat schedule
        of the periodic entries. A message runs ``run(name, payload)``.
        Celery-only, until ``idk-worker-port`` moves it behind the host."""
        register_task_table(self._app, table, run)

    async def health(self) -> HealthResult:
        """One bounded connection to this instance's broker
        (:func:`~sandloom.core.drivers.celery_broker_probe.probe_broker`);
        building the provider sends nothing, so without it a broker outage
        was invisible."""
        return await probe_broker(self._app)

    async def raw_backend(self) -> Any:
        return self._app


def _maybe_published(exc: Exception, instance: str) -> Unavailable | Timeout:
    """A broker failure after the kwargs encoded (spec 8.2): ``Timeout`` for
    any ``TimeoutError`` (builtin / ``socket.timeout``, redis-py's, by name so
    no client is imported), ``Unavailable`` for the rest (kombu's
    ``OperationalError``, a dropped connection, a lost ack). Either may have
    reached the broker: the kernel keeps the dedupe key pending."""
    detail = f"task publish failed ({type(exc).__name__}); it may have been published"
    if any(cls.__name__ == "TimeoutError" for cls in type(exc).__mro__):
        return Timeout(detail, capability="taskqueue", instance=instance)
    return Unavailable(detail, capability="taskqueue", instance=instance)


def create_celery_taskqueue(cfg: InstanceConfig) -> CeleryTaskQueueProvider:
    """Driver factory for ``taskqueue`` capability / ``celery`` driver.

    The ``default`` instance uses the process-wide default app (built from its
    options the first time); a named instance, or a ``default`` one whose
    broker or backend differ from the default app's, builds its own.
    """
    options = CeleryTaskQueueOptions.model_validate(cfg.options)
    if not options.broker:
        raise ValueError(
            f"celery taskqueue instance {cfg.name!r} needs the 'broker' option "
            "(the kernel fills it from BROKER_URL)"
        )
    # No backend: no result backend (the framework stores no task result).
    broker, backend = options.broker, options.backend

    if cfg.name == DEFAULT_INSTANCE and (
        _default_app is None or _default_app_urls == (broker, backend)
    ):
        app = get_default_app(broker, backend)
    else:
        from celery import Celery  # local import: optional heavy dependency  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        app = _apply_framework_defaults(
            Celery(f"sandloom-{cfg.name}", broker=broker, backend=backend)
        )

    return CeleryTaskQueueProvider(
        name=cfg.name,
        driver=cfg.driver,
        options=dict(cfg.options),
        app=app,
    )


__all__ = [
    "CeleryTaskQueueProvider",
    "CeleryTaskQueueOptions",
    "create_celery_taskqueue",
    "get_default_app",
    "register_task_table",
]
