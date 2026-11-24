"""The ``taskqueue`` capability (spec section 8).

Two doors. The module door is the task API of ``sandloom.sdk.tasks``
(``@task``, ``@periodic``) and the by-name :class:`TaskQueue` below, both
served by the kernel layer above the driver (:mod:`sandloom.core.tasks`),
because enqueue dedupe is a kernel guarantee (G-TASK-01..03). The driver door
is :meth:`TaskQueueProvider.send`: it publishes one message under the task id
the kernel chose, nothing else. So the provider ABC does not subclass the
module Protocol (ADR-0016 records this exception to ADR-0011 decision 1).

The worker side is the third door: :meth:`TaskQueueProvider.worker_host` answers
a :class:`WorkerHost`, which runs the worker process and the scheduler over a
:class:`WorkerBoot` the kernel implements (``sandloom.core.worker``). A host is
not a capability: it has no ``INFRA`` entry and no major of its own, it is
versioned with ``taskqueue``.
"""

from __future__ import annotations

import abc
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar, Protocol, TypeVar

from pydantic import BaseModel

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider
from sandloom.core.infra.ports.lease import LeaseStore

T = TypeVar("T")


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


class WorkerBoot(Protocol):
    """What the kernel gives a worker host (the kernel implements it, the host
    calls it). Every method is synchronous: a host is a blocking process.

    A host calls :meth:`boot` once in its main process, before it forks or
    starts consuming, then :meth:`before_fork`; :meth:`run` for each message;
    :meth:`on_process_start` / :meth:`on_process_stop` in each process that runs
    messages (the main one for a host that forks none).
    """

    def boot(self) -> TaskTable:
        """Load the composition, enforce the runtime checks, discover the tasks
        of the loaded modules; answer the table the host registers. Raises when
        the loaded modules need a feature the driver lacks (SANDLOOM-B037)."""
        ...

    def run(self, task: str, payload: bytes, *, task_id: str) -> None:
        """Run one message: ``task`` with the JSON ``payload``, outside any
        running event loop. An ``async def`` body runs on the process's one
        persistent loop, a sync body directly; the body's own exception
        propagates (the host decides on retry or redelivery). Raises
        ``InvalidArgument`` for an unknown task or a payload that does not
        validate."""
        ...

    def before_fork(self) -> None:
        """Called once in the host's main process after :meth:`boot`, right
        before it forks processes that run messages (a host that forks none
        does not call it): the kernel freezes the startup heap so children
        share it."""
        ...

    def on_process_start(self) -> None:
        """A process that runs messages starts (a fresh loop after a fork)."""
        ...

    def on_process_stop(self) -> None:
        """A process that ran messages stops: dispose the engines and clients on
        the loop that opened them, then close it (G-TASK-04)."""
        ...

    def run_coroutine(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion on the process's persistent loop (outside
        any running loop). A scheduler uses it for its lease operations, so the
        lease store is opened, used and disposed on one loop."""
        ...


class WorkerHost(Protocol):
    """The driver's worker process and scheduler (``TaskQueueProvider.worker_host``).

    A host is not a capability: no ``INFRA`` entry, no major of its own.
    """

    def run_worker(
        self,
        boot: WorkerBoot,
        *,
        queues: Sequence[str] | None = None,
        concurrency: int | None = None,
        loglevel: str = "INFO",
    ) -> int:
        """Consume the queues (``None``: the default queue) and run each message
        through ``boot.run`` until the process is told to stop; ``loglevel`` is
        the log level of the process. The process model that runs the messages
        (child processes, one process, threads) is the driver's own option, read
        from its INFRA options: a configuration the host cannot run raises
        ``InvalidArgument`` before it consumes. Blocks; the return value is the
        process exit code (non-zero when the worker died)."""
        ...

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
        """Send each periodic task of ``boot.boot().periodic`` at its cron slots,
        only while this process holds ``lease_name`` on ``lease`` (a fenced
        lease of ``lease_ttl_seconds``: the kernel computes name and store, so a
        second scheduler stands by). ``state_file`` is where a scheduler that
        keeps state between runs keeps it (``None``: the driver's default; a
        driver that keeps none ignores it). Blocks; the exit code of the
        process."""
        ...

    def check(self, *, timeout_seconds: float) -> HealthResult:
        """Whether a worker of this driver runs: ``ok`` when it reported within
        ``timeout_seconds``, else ``fail``. It boots nothing and takes no broker
        round trip that a container healthcheck could not afford; the details
        carry no broker address or credential."""
        ...


class TaskQueueProvider(InfraProvider):
    """Background job broker capability (e.g. Celery, RQ, Dramatiq).

    The driver door is :meth:`send` (publish one message) and
    :meth:`worker_host` (the process that runs the messages). A driver states
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

    @abc.abstractmethod
    def worker_host(self) -> WorkerHost:
        """The host that runs this instance's worker process and scheduler.

        Synchronous and cheap: it builds the host over what the instance
        already holds, and connects to nothing. Not a capability: the kernel
        reaches a host only through this method.
        """


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
        # deployment runs (G-INFRA-11); the scheduler can send a slot twice,
        # so the module tolerates duplicates. A module with a ``@periodic``
        # task needs it (derived at load, declared: SANDLOOM-C033).
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
    "WorkerBoot",
    "WorkerHost",
]
