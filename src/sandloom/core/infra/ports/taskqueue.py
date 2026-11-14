"""The ``taskqueue`` capability (spec section 8).

Two doors. The module door is the task API of ``sandloom.sdk.tasks``
(``@task``, ``@periodic``) and the by-name :class:`TaskQueue` below, both
served by the kernel layer above the driver (:mod:`sandloom.core.tasks`),
because enqueue dedupe is a kernel guarantee (G-TASK-01..03). The driver door
is :meth:`TaskQueueProvider.send`: it publishes one message under the task id
the kernel chose, nothing else. So the provider ABC does not subclass the
module Protocol (ADR-0016 records this exception to ADR-0011 decision 1).

``idk-worker-port`` adds ``WorkerBoot``, ``WorkerHost`` and the abstract
``worker_host()`` to this file; until then ``sandloom.core.worker`` runs the
:class:`TaskTable` through Celery directly.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider


class TaskQueue(Protocol):
    """What a module calls on ``taskqueue()``: enqueue a task by its name."""

    async def enqueue(
        self,
        task: str,
        payload: BaseModel | Mapping[str, Any] | None,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """What the driver's host needs to know of one task."""

    queue: str = "default"
    at_least_once: bool = False


@dataclass(frozen=True, slots=True)
class PeriodicSpec:
    """One periodic task: ``task`` (a task name) runs at the slots of ``cron``
    (five fields, UTC); ``id`` is unique in the deployment."""

    id: str
    cron: str
    task: str
    queue: str = "default"


@dataclass(frozen=True, slots=True)
class TaskTable:
    """The tasks of the loaded modules, derived from their declarations."""

    tasks: Mapping[str, TaskSpec]
    periodic: Sequence[PeriodicSpec]


class TaskQueueProvider(InfraProvider):
    """Background job broker capability (e.g. Celery, RQ, Dramatiq).

    The driver door is one operation: :meth:`send`. A driver states
    ``contract_version = 2``. Modules never reach a driver: they enqueue
    through the kernel layer, which validates, deduplicates and chooses the
    task id.
    """

    capability: ClassVar[str] = "taskqueue"

    @abc.abstractmethod
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
        """Publish one message for ``task`` under ``task_id``; return the id.

        ``queue`` is the task's declared queue: the message goes there, so an
        enqueue from a process that never registered the task table (an API
        process) reaches the same queue the worker consumes.

        ``payload`` is the task's JSON-encoded payload object (``b"{}"`` for a
        task without one). At most one publish per call, and never a retry of
        a publish on the driver's own. Raises ``InvalidArgument`` (unknown
        task, a payload the driver cannot carry) or ``TooLarge`` when nothing
        was published; ``Unavailable`` or ``Timeout`` when the publish may have
        happened; ``Unsupported("delay")`` for a ``delay`` without the feature
        ``delay``; ``PermissionDenied``.
        """

    async def raw_backend(self) -> Any:
        """Escape hatch: return the underlying broker/app, if the driver has one.

        NOT part of the portable contract (``idk-worker-port`` removes it with
        the Celery-only halves of ``sandloom.core.worker``).
        """
        raise NotImplementedError(
            f"taskqueue driver {self.driver!r} does not expose a raw backend"
        )


TASKQUEUE_CAPABILITY = CapabilitySpec(
    name="taskqueue",
    # Modules see the shape of v1; drivers owe ``send`` of major 2.
    version="v1",
    major=2,
    provider_contract=TaskQueueProvider,
    feature_kinds={
        # ``send(delay=)``.
        "delay": "behaviour",
        # The driver's host runs the scheduler in at least one process while the
        # deployment runs (G-INFRA-11); a run may repeat, so the module
        # tolerates duplicates. A module with a ``@periodic`` task needs it
        # (derived at load, declared: SANDLOOM-C033).
        "periodic": "guarantee",
        # An ``at_least_once`` task is redelivered when its worker dies.
        "redelivery": "guarantee",
    },
    description="Background job broker (send; periodic jobs with 'periodic').",
)


__all__ = [
    "TASKQUEUE_CAPABILITY",
    "PeriodicSpec",
    "TaskQueue",
    "TaskQueueProvider",
    "TaskSpec",
    "TaskTable",
]
