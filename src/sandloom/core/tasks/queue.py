"""The kernel's taskqueue layer: what a module's ``taskqueue()`` returns.

A module enqueues through :class:`KernelTaskQueue`, never on the driver: the
layer serves ``idempotency_key`` (:func:`sandloom.core.tasks.enqueue.enqueue_task`,
G-TASK-03) and the driver only sends. ``sandloom.sdk.infra.taskqueue()``,
``sandloom.core.di.taskqueue()`` and ``CoreRuntime.taskqueue()`` return it, and
so does every resolution by capability name a module can make
(:func:`module_facing`); the kernel's own facade (``CoreFacade.taskqueue``,
the worker) keeps the provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, TypeVar, cast

from sandloom.core.infra import access as _access
from sandloom.core.infra.capabilities import CapabilityRef
from sandloom.core.infra.contracts import TaskQueue, TaskQueueProvider
from sandloom.core.tasks.enqueue import enqueue_task

P = TypeVar("P")


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
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Enqueue the declared task ``task`` (by name) with ``payload``.

        ``delay`` (feature ``delay``) schedules it for later. With
        ``idempotency_key``, a successful repeat returns the
        original job id within the instance's replay window and different
        arguments conflict (409); a concurrent or ambiguous send fails closed
        (503 ``task_enqueue_uncertain``) instead of publishing again.
        """
        return await enqueue_task(
            self._provider,
            task,
            payload,
            delay=delay,
            idempotency_key=idempotency_key,
        )


def taskqueue(name: str = "default") -> TaskQueue:
    """Return the taskqueue instance ``name`` behind the kernel's layer."""
    provider = cast("TaskQueueProvider", _access.taskqueue(name))
    return KernelTaskQueue(provider)


def module_facing(capability: str, provider: Any) -> Any:
    """``provider`` as module code may hold it: a ``taskqueue`` instance behind
    :class:`KernelTaskQueue`, any other capability unchanged.

    Every module-facing resolution by capability name goes through here
    (``CoreRuntime.capability`` / ``require_capability``, DI ``Capability``,
    ``sandloom.sdk.infra.capability``): the raw provider's
    ``send(task_id=)`` would let a module publish under the job id of a
    pending dedupe reservation. No I/O: one wrapper object.
    """
    if capability == "taskqueue":
        return KernelTaskQueue(cast("TaskQueueProvider", provider))
    return provider


def capability(ref: CapabilityRef[P], name: str = "default") -> P:
    """Return the instance ``name`` of the capability ``ref`` points to, typed by the
    ref. A capability's package publishes the ref and the caller's Protocol, and the
    capability's major version must match the ref's. A ``taskqueue`` instance
    comes behind the kernel's layer, as from ``taskqueue()``."""
    return cast("P", module_facing(ref.name, _access.capability(ref, name)))


__all__ = ["KernelTaskQueue", "capability", "module_facing", "taskqueue"]
