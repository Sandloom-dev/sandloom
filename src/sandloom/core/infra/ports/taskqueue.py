"""The ``taskqueue`` capability (module-facing; spec section 8).

Until ``idk-task-api`` and ``idk-worker-port`` replace it here (``send``,
``WorkerHost``), this is the taskqueue contract the kernel has used so far,
moved here unchanged from :mod:`sandloom.core.infra.contracts` (which
re-exports it), with the capability's spec.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider


class TaskQueue(Protocol):
    """What a module calls on ``taskqueue()``."""

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...


class TaskQueueProvider(InfraProvider, TaskQueue):
    """Background job broker capability (e.g. Celery, RQ, Dramatiq, in-memory).

    The contract is the single operation callers actually need — *enqueue a
    named task with a payload* — never the underlying broker/app object. A new
    broker is plug-compatible the moment it implements :meth:`enqueue`; callers
    cannot couple to Celery's ``send_task``/``.delay`` surface.
    """

    capability: ClassVar[str] = "taskqueue"

    @abc.abstractmethod
    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Enqueue ``task`` (by registered name) with keyword ``payload``.

        ``delay_seconds`` schedules the task for later. With ``idempotency_key``
        (feature ``idempotency_key``), successful repeats return the original job
        id within the driver's replay window; different arguments conflict.
        Concurrent or ambiguous sends fail closed instead of publishing again.
        This requires a shared durable dedupe store and does NOT deduplicate
        broker redelivery or task effects. Returns the broker's task/job id.
        """

    async def raw_backend(self) -> Any:
        """Escape hatch: return the underlying broker/app, if the driver has one.

        NOT part of the portable contract — only backends with a meaningful app
        object (e.g. Celery) override this. The default raises so portable code
        never depends on it.
        """
        raise NotImplementedError(
            f"taskqueue driver {self.driver!r} does not expose a raw backend"
        )


TASKQUEUE_CAPABILITY = CapabilitySpec(
    name="taskqueue",
    version="v1",
    provider_contract=TaskQueueProvider,
    # ``periodic`` is a guarantee, not a method: the driver runs a module's
    # periodic jobs (its beat hook) at their interval in at least one process
    # while the deployment runs; a run may repeat, so the module tolerates
    # duplicates. A driver never advertises it for supporting enqueue alone;
    # only ``celery`` (worker, beat and the beat lease) does today. A module
    # with a beat hook needs it (derived at load, declared: SANDLOOM-C033).
    feature_kinds={
        "delay": "behaviour",
        "idempotency_key": "behaviour",
        "periodic": "guarantee",
    },
    description="Background job broker (enqueue; periodic jobs with 'periodic').",
)


__all__ = ["TASKQUEUE_CAPABILITY", "TaskQueue", "TaskQueueProvider"]
