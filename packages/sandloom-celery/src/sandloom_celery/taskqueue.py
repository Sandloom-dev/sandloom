"""Celery task-queue driver (``sandloom-celery``).

This driver is the SOLE constructor of Celery apps, including the process-wide
``default`` one (:func:`~sandloom_celery.worker.get_default_app`).
Named instances build their own Celery app from ``options['broker']`` /
``options['backend']``. The kernel only depends on the abstract
:class:`TaskQueueProvider` — modules never import Celery directly. The worker
and the scheduler are this driver's host (:meth:`CeleryTaskQueueProvider.
worker_host`, :mod:`sandloom_celery.worker`): it runs the SAME app,
so enqueue and execution share one namespace.

The driver reads no kernel setting (its ``broker`` and ``backend`` are options
of the instance; the kernel still fills what an instance leaves out from the
optional ``BROKER_URL`` / ``CACHE_URL``) and only publishes: enqueue
dedupe (``idempotency_key``) is the kernel's taskqueue layer
(:mod:`sandloom.core.tasks.queue`), which reserves a job id and hands it to
:meth:`CeleryTaskQueueProvider.send` as ``task_id``. A failure that provably
published nothing is :class:`InvalidArgument`; anything else may have reached
the broker and is :class:`Unavailable` or :class:`Timeout` (spec 8.2).
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Self, TypeVar
from urllib.parse import urlsplit

import orjson
from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    DEFAULT_INSTANCE,
    HealthResult,
    InstanceConfig,
    InvalidArgument,
    SettingsFill,
    TaskQueueProvider,
    Timeout,
    Unavailable,
)
from sandloom.driverkit.testing_isolation import isolated_redis_options
from sandloom_celery.broker_probe import BrokerUnreachableError, probe_broker
from sandloom_celery.worker import (
    CeleryWorkerHost,
    WorkerPool,
    build_named_app,
    default_app_matches,
    get_default_app,
)


class MissingBrokerError(ValueError):
    """A ``celery`` task-queue instance has no ``broker``: a configuration
    mistake that aborts startup in every environment (``fatal_at_startup``),
    never a transient fault."""

    fatal_at_startup = True


#: Features the Celery task-queue driver supports. ``periodic``: the worker
#: registers each periodic task of the table and one beat per broker and module
#: set runs it under a fenced lease (``sandloom_celery.worker``).
#: ``redelivery``: an ``at_least_once`` task is registered ``acks_late`` and
#: ``reject_on_worker_lost``.
CELERY_TASKQUEUE_FEATURES = frozenset({"delay", "periodic", "redelivery"})


class CeleryTaskQueueOptions(BaseModel):
    """Config-shape schema for the ``celery`` task-queue driver.

    The driver reads ``broker`` (message broker URL) and ``backend`` (result
    backend URL), both options of the instance (``INFRA.taskqueue.<name>``);
    the kernel still fills what the instance leaves out from the optional flat
    settings ``BROKER_URL`` / ``CACHE_URL`` while they exist, and an instance
    with no ``broker`` refuses startup. ``dedupe_ttl_seconds`` is the
    replay window of the kernel's enqueue dedupe for this instance.
    ``extra="forbid"`` rejects a typo'd key at startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    broker: str | None = None
    backend: str | None = None
    #: The kernel's record TTL (``RECORD_TTL_SECONDS``, one day), written out:
    #: the driver imports no kernel module (test_celery_replay_limits keeps them equal).
    dedupe_ttl_seconds: int = Field(default=24 * 60 * 60, ge=1, le=604800)
    #: The bound of the broker probe behind ``health()`` (connect and socket
    #: timeouts included); the kernel fills it from ``HEALTH_CHECK_TIMEOUT_SECONDS``
    #: (``settings_fill``), and never above it (the readiness probe's own
    #: bound): the driver reads no kernel setting.
    health_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    #: How the worker host runs the messages: ``prefork`` (child processes),
    #: ``solo`` (in the worker process) or ``threads``. On macOS billiard cannot
    #: fork its children: set ``solo`` or ``threads`` there.
    worker_pool: WorkerPool = "prefork"


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


_Q = TypeVar("_Q", bound="CeleryTaskQueueProvider")


class CeleryTaskQueueProvider(TaskQueueProvider):
    """A single Celery application instance."""

    contract_version = 2
    supported_features = CELERY_TASKQUEUE_FEATURES
    options_schema = CeleryTaskQueueOptions
    # The optional flat ``BROKER_URL`` fills the broker and ``CACHE_URL`` the
    # result backend of an instance that states none; the probe bound is
    # ``HEALTH_CHECK_TIMEOUT_SECONDS``, and never above it.
    settings_fill = SettingsFill(
        urls={"broker": "broker_url", "backend": "cache_url"},
        bounds={"health_timeout_seconds": "health_check_timeout_seconds"},
        caps={"health_timeout_seconds": "health_check_timeout_seconds"},
    )
    required_imports = ("celery",)
    install_hint = "pip install 'sandloom[celery]'"
    # Beta, the floor for a driver of this repository (spec 2.5): the
    # `celery-drivers` CI lane runs TaskQueueConformance (send and host groups)
    # against the built wheel and a real Redis broker
    # (`packages/sandloom-celery/tests/test_taskqueue_kit*`). An undeclared
    # tier would be experimental.
    tier = "beta"

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """Another database index of the Redis ``broker`` and ``backend`` URLs
        for each xdist worker (a ``memory://`` broker needs none)."""
        return isolated_redis_options(options, worker_id, keys=("broker", "backend"))

    @classmethod
    def from_config(cls, config: InstanceConfig) -> Self:
        return _build_celery_taskqueue(cls, config)

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        app: Any | None = None,
        app_factory: Callable[[], Any] | None = None,
        validated: CeleryTaskQueueOptions | None = None,
    ) -> None:
        if (app is None) == (app_factory is None):
            raise ValueError("pass exactly one of app and app_factory")
        super().__init__(name=name, driver=driver, options=options)
        self._built_app = app
        self._app_factory: Callable[[], Any] = app_factory or (lambda: app)
        # The factory hands over the object it already validated: ``options``
        # is not parsed a second time.
        self._typed = validated or CeleryTaskQueueOptions.model_validate(options)

    @property
    def _app(self) -> Any:
        """The Celery app, built on first use: the provider is built for a
        health check or ``sandloom worker check`` too, which import no Celery."""
        if self._built_app is None:
            self._built_app = self._app_factory()
        return self._built_app

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
        from celery.exceptions import NotRegistered
        from kombu.exceptions import (  # type: ignore[import-untyped]
            ContentDisallowed,
            EncodeError,
        )
        from kombu.serialization import dumps  # type: ignore[import-untyped]

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

    def worker_host(self) -> CeleryWorkerHost:
        """The Celery host of this instance's app: it consumes the queues the
        kernel's task table declares and runs the beat under the kernel's
        lease (:mod:`sandloom_celery.worker`). Connects to
        nothing."""
        return CeleryWorkerHost(lambda: self._app, worker_pool=self._typed.worker_pool)

    async def health(self) -> HealthResult:
        """One bounded connection to this instance's broker
        (:func:`~sandloom_celery.broker_probe.probe_broker`);
        building the provider sends nothing, so without it a broker outage
        was invisible. A broker that refuses the connection is a failed
        probe carrying the error's type name (spec 2.4), never its text."""
        try:
            return await probe_broker(
                self._app, limit_seconds=self._typed.health_timeout_seconds
            )
        except BrokerUnreachableError as exc:
            return HealthResult(status="fail", details={"error": type(exc).__name__})


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
    return _build_celery_taskqueue(CeleryTaskQueueProvider, cfg)


def _build_celery_taskqueue(cls: type[_Q], cfg: InstanceConfig) -> _Q:
    """``cls`` over the app the instance's options describe (so a subclass's
    ``from_config`` returns the subclass)."""
    options = CeleryTaskQueueOptions.model_validate(cfg.options)
    if not options.broker:
        example = {"taskqueue": {cfg.name: {"broker": "redis://localhost:6379/1"}}}
        raise MissingBrokerError(
            f"celery taskqueue instance {cfg.name!r} has no 'broker' option: set "
            f"the INFRA option taskqueue.{cfg.name}.broker, e.g. "
            f"INFRA='{json.dumps(example)}'"
        )
    # No backend: no result backend (the framework stores no task result).
    broker, backend = options.broker, options.backend

    # The app is built on first use, but a missing Celery fails here, at build:
    # the registry answers it as an unavailable driver (a lookup, not an import).
    if importlib.util.find_spec("celery") is None:
        raise ModuleNotFoundError("No module named 'celery'", name="celery")

    def build() -> Any:
        if cfg.name == DEFAULT_INSTANCE and default_app_matches(broker, backend):
            return get_default_app(broker, backend)
        return build_named_app(cfg.name, broker, backend)

    # The app is built on first use (a publish, a probe, the worker host).
    return cls(
        name=cfg.name,
        driver=cfg.driver,
        options=dict(cfg.options),
        app_factory=build,
        validated=options,
    )


__all__ = [
    "CeleryTaskQueueProvider",
    "CeleryTaskQueueOptions",
    "create_celery_taskqueue",
]
