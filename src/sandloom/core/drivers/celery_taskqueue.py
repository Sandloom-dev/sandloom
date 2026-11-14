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
:meth:`CeleryTaskQueueProvider.enqueue` as ``task_id``. A failure that provably
published nothing is :class:`InvalidArgument`; anything else may have reached
the broker and is :class:`Unavailable` or :class:`Timeout` (spec 8.2).
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field

from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.celery_broker_probe import probe_broker
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import TaskQueueProvider
from sandloom.core.infra.errors import InvalidArgument, Timeout, Unavailable

#: Features the Celery task-queue driver supports. ``periodic``: the worker
#: connects each loaded module's beat hook and one beat per broker and module
#: set runs it under a fenced lease (sandloom.core.worker, beat_lease); the
#: only driver that advertises it today.
CELERY_TASKQUEUE_FEATURES = frozenset({"delay", "idempotency_key", "periodic"})


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
#: Threads that run the blocking broker publish of ``enqueue``.
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


class CeleryTaskQueueProvider(TaskQueueProvider):
    """A single Celery application instance."""

    contract_version = 1
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

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """Publish ``task`` once, under ``task_id`` when given (the broker's
        job id; the kernel's taskqueue layer reserves it).

        ``idempotency_key`` is served by the kernel's taskqueue layer, never
        by the driver: passed here directly it is refused with
        :class:`InvalidArgument` before anything is sent, so a caller that
        bypasses the layer never publishes without the dedupe it asked for.
        A failure that provably published nothing (the kwargs cannot be
        encoded, the task is unknown) is :class:`InvalidArgument`; anything
        else (connection drop, timeout, lost ack) may have reached the broker
        and is :class:`Unavailable` or :class:`Timeout`.
        """
        if idempotency_key is not None:
            raise InvalidArgument(
                "idempotency_key is served by the kernel's taskqueue layer "
                "(sandloom.sdk.infra.taskqueue()), not by the driver",
                capability="taskqueue",
                instance=self.name,
            )
        return await self._send(task, deepcopy(dict(payload)), delay_seconds, task_id)

    async def _send(
        self,
        task: str,
        payload: dict[str, Any],
        delay_seconds: int | None,
        task_id: str | None,
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
            countdown=delay_seconds,
            task_id=task_id,
            ignore_result=True,
            retry=False,
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
        except Exception as exc:  # noqa: BLE001 - may have published: mapped, never swallowed
            raise _maybe_published(exc, self.name) from exc

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
]
