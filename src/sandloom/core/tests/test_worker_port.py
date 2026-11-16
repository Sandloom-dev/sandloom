"""The worker port of the task queue (spec 8.2), the kernel's side.

A task-queue driver answers ``worker_host`` and exposes no backend handle. The
Celery host that implements it (registration, heartbeat, scheduler under the
lease) is tested where the driver lives, in
``packages/sandloom-celery/tests/test_celery_worker_host.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.infra.ports.taskqueue import TaskQueueProvider


def test_the_provider_door_has_a_worker_host_and_no_escape_hatch() -> None:
    assert "worker_host" in TaskQueueProvider.__abstractmethods__
    assert not hasattr(TaskQueueProvider, "raw_backend")


def test_a_driver_without_a_host_cannot_be_built() -> None:
    class _NoHost(TaskQueueProvider):
        async def send(self, *args: Any, **kwargs: Any) -> str:
            return "x"

    with pytest.raises(TypeError, match="worker_host"):
        _NoHost(name="n", driver="d", options={})  # type: ignore[abstract]
