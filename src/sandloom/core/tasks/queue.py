"""The kernel's taskqueue layer: what a module's ``taskqueue()`` returns.

A module enqueues through :class:`KernelTaskQueue`, never on the driver: the
layer serves ``idempotency_key`` (:func:`sandloom.core.tasks.enqueue.enqueue_task`,
G-TASK-03) and the driver only publishes. ``sandloom.sdk.infra.taskqueue()``,
``sandloom.core.di.taskqueue()`` and ``CoreRuntime.taskqueue()`` return it; the
kernel's own facade (``CoreFacade.taskqueue``, the worker) keeps the provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from sandloom.core.infra import access as _access
from sandloom.core.infra.contracts import TaskQueue, TaskQueueProvider
from sandloom.core.tasks.enqueue import enqueue_task


class KernelTaskQueue:
    """A taskqueue instance as a module sees it (the ``TaskQueue`` Protocol)."""

    __slots__ = ("_provider",)

    def __init__(self, provider: TaskQueueProvider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        """The instance name."""
        return self._provider.name

    @property
    def driver(self) -> str:
        """The instance's driver."""
        return self._provider.driver

    def features(self) -> frozenset[str]:
        """The features the instance declares."""
        return self._provider.features()

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Enqueue ``task`` (by registered name) with keyword ``payload``.

        ``delay_seconds`` schedules it for later. With ``idempotency_key``
        (feature ``idempotency_key``), a successful repeat returns the
        original job id within the instance's replay window and different
        arguments conflict (409); a concurrent or ambiguous send fails closed
        (503 ``task_enqueue_uncertain``) instead of publishing again.
        """
        return await enqueue_task(
            self._provider,
            task,
            payload,
            delay_seconds=delay_seconds,
            idempotency_key=idempotency_key,
        )


def taskqueue(name: str = "default") -> TaskQueue:
    """Return the taskqueue instance ``name`` behind the kernel's layer."""
    provider = cast("TaskQueueProvider", _access.taskqueue(name))
    return KernelTaskQueue(provider)


__all__ = ["KernelTaskQueue", "taskqueue"]
