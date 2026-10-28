"""Built-in Celery task-queue driver.

This driver is the SOLE constructor of Celery apps, including the process-wide
``default`` one (:func:`get_default_app`). Named instances build their own
Celery app from ``options['broker']`` / ``options['backend']``. The kernel only
depends on the abstract :class:`TaskQueueProvider` — modules never import Celery
directly. The framework worker (``sandloom.core.worker``) consumes the SAME app,
resolved through the registry, so enqueue and execution share one namespace.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from sandloom.core.config.settings import settings
from sandloom.core.effects import RECORD_TTL_SECONDS
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import TaskQueueProvider
from sandloom.core.tasks.enqueue import TaskNotSent, enqueue_once

#: Features the Celery task-queue driver supports.
CELERY_TASKQUEUE_FEATURES = frozenset({"enqueue", "delay", "idempotency_key"})


class CeleryTaskQueueOptions(BaseModel):
    """Config-shape schema for the ``celery`` task-queue driver.

    The driver reads ``broker`` (message broker URL) and ``backend`` (result
    backend URL); both are optional (default to ``BROKER_URL`` / ``CACHE_URL``).
    ``extra="forbid"`` rejects a typo'd key at startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    broker: str | None = None
    backend: str | None = None
    dedupe_ttl_seconds: int = Field(default=RECORD_TTL_SECONDS, ge=1, le=604800)


#: Process-wide ``default`` Celery app, built lazily from settings.
_default_app: Any | None = None
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


def get_default_app() -> Any:
    """Return the process-wide ``default`` Celery app (built once)."""
    global _default_app
    if _default_app is None:
        from celery import Celery  # local import: optional heavy dependency  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        _default_app = _apply_framework_defaults(
            Celery("project", broker=settings.broker_url, backend=settings.cache_url)
        )
    return _default_app


class CeleryTaskQueueProvider(TaskQueueProvider):
    """A single Celery application instance."""

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

    def features(self) -> frozenset[str]:
        return CELERY_TASKQUEUE_FEATURES

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        payload = deepcopy(dict(payload))
        # A serializer error is a definite pre-send failure. Detect it before
        # enqueue_once reserves a durable "possibly sent" key; that reservation
        # must be retained only for genuinely ambiguous broker failures.
        from kombu.serialization import dumps  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        serializer = (
            getattr(getattr(self._app, "conf", None), "task_serializer", None) or "json"
        )
        dumps(payload, serializer=serializer)
        if idempotency_key is not None:
            options = CeleryTaskQueueOptions.model_validate(self.options)
            broker = urlsplit(options.broker or settings.broker_url)
            # Credential rotation must not open a new dedupe namespace.
            namespace = repr(
                (self.name, broker.scheme, broker.hostname, broker.port, broker.path)
            )
            return await enqueue_once(
                namespace=namespace,
                key=idempotency_key,
                task=task,
                payload=payload,
                delay_seconds=delay_seconds,
                ttl_seconds=options.dedupe_ttl_seconds,
                send=lambda task_id: self._send(
                    task, payload, delay_seconds, task_id, classify=True
                ),
            )
        return await self._send(task, payload, delay_seconds, None)

    async def _send(
        self,
        task: str,
        payload: Mapping[str, Any],
        delay_seconds: int | None,
        task_id: str | None,
        *,
        classify: bool = False,
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
            kwargs=dict(payload),
            countdown=delay_seconds,
            task_id=task_id,
            ignore_result=True,
            retry=False,
        )
        if classify:
            send = functools.partial(self._send_classified, send, dict(payload))
        result = await asyncio.get_running_loop().run_in_executor(
            _enqueue_executor(), send
        )
        return str(getattr(result, "id", result))

    def _send_classified(self, send: Any, kwargs: dict[str, Any]) -> Any:
        """``send`` with provably pre-broker failures raised as TaskNotSent.

        The kwargs are encoded with the app's task serializer first (what
        kombu does before any network write); kombu's own encode errors and
        an unknown task from ``send_task`` are pre-broker too. Everything else
        (connection drop, timeout, lost ack, any I/O error) may have reached
        the broker and propagates unchanged — the caller keeps the dedupe key
        pending.
        """
        from celery.exceptions import NotRegistered  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver
        from kombu.exceptions import (  # type: ignore[import-untyped]  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver
            ContentDisallowed,
            EncodeError,
        )
        from kombu.serialization import dumps  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        serializer = str(self._app.conf.task_serializer or "json")
        try:
            dumps(kwargs, serializer=serializer)
        except Exception as exc:  # nothing was sent
            raise TaskNotSent(f"task kwargs cannot be encoded: {exc}") from exc
        try:
            return send()
        except (EncodeError, ContentDisallowed, NotRegistered) as exc:
            raise TaskNotSent(f"{type(exc).__name__}: {exc}") from exc

    async def raw_backend(self) -> Any:
        return self._app


def create_celery_taskqueue(cfg: InstanceConfig) -> CeleryTaskQueueProvider:
    """Driver factory for ``taskqueue`` capability / ``celery`` driver."""
    broker = str(cfg.options.get("broker") or settings.broker_url)
    backend = str(cfg.options.get("backend") or settings.cache_url)

    if (
        cfg.name == DEFAULT_INSTANCE
        and broker == settings.broker_url
        and backend == settings.cache_url
    ):
        app = get_default_app()
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
