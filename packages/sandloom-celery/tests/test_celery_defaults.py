"""The framework Celery defaults of the ``celery`` taskqueue driver: no result
stored, one message prefetched, ``at_least_once`` a per-task opt-in, and what
``send`` hands the broker."""

from __future__ import annotations

from typing import Any

import pytest
from celery import Celery
from sandloom_celery import taskqueue as celery_taskqueue
from sandloom_celery import worker as celery_worker

from sandloom.driverkit import InstanceConfig


def test_worker_reliability_defaults() -> None:
    app = Celery("t", broker="memory://", backend="cache+memory://")
    celery_worker.configure_worker_reliability(app)
    assert app.conf.task_ignore_result is True
    assert app.conf.worker_prefetch_multiplier == 1
    # acks_late stays an explicit per-task opt-in (idempotent tasks only).
    assert not app.conf.task_acks_late


def test_driver_built_apps_ignore_results_by_default() -> None:
    provider = celery_taskqueue.create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="named",
            driver="celery",
            options={"broker": "memory://", "backend": "cache+memory://"},
        )
    )
    assert provider._app.conf.task_ignore_result is True


@pytest.mark.asyncio
async def test_enqueue_never_subscribes_a_result_consumer() -> None:
    sent: list[dict[str, Any]] = []

    class _App:
        def send_task(self, name: str, **options: Any) -> Any:
            sent.append({"name": name, **options})
            return type("R", (), {"id": "task-1"})()

    provider = celery_taskqueue.CeleryTaskQueueProvider(
        name="x", driver="celery", options={}, app=_App()
    )
    assert await provider.send("mod.task", b'{"a":1}', task_id="task-1") == "task-1"
    assert sent == [
        {
            "name": "mod.task",
            "kwargs": {"a": 1},
            "countdown": None,
            "task_id": "task-1",
            "ignore_result": True,
            "retry": False,
        }
    ]


@pytest.mark.asyncio
async def test_enqueue_carries_the_declared_queue_without_a_task_table() -> None:
    """A producer that never registered the table (an API process) still
    routes the message to the task's queue."""
    sent: list[dict[str, Any]] = []

    class _App:
        def send_task(self, name: str, **options: Any) -> Any:
            sent.append({"name": name, **options})
            return type("R", (), {"id": "task-1"})()

    provider = celery_taskqueue.CeleryTaskQueueProvider(
        name="x", driver="celery", options={}, app=_App()
    )
    await provider.send("mod.task", b"{}", task_id="task-1", queue="slow")
    assert sent[0]["queue"] == "slow"
