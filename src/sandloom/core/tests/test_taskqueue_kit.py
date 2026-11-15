"""``TaskQueueConformance`` send groups (spec 8.2): the celery driver passes them
on an in-memory broker, and the kit catches broken drivers.

The host groups (forked worker and scheduler processes, about a minute) are in
``test_taskqueue_kit_host.py``, and the kit's checks against broken hosts in
``test_taskqueue_kit_hostdefects.py``: ``--dist loadfile`` runs one file on one
worker, so three files run on three.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.drivers.celery_taskqueue import create_celery_taskqueue
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.driverkit.testing import TaskQueueConformance

#: The groups that fork a host: their own file (``test_taskqueue_kit_host.py``).
_HOST_GROUP_PREFIXES = ("test_host_", "test_scheduler_", "test_check_")


def _celery(broker: str) -> TaskQueueProvider:
    return create_celery_taskqueue(
        InstanceConfig("taskqueue", "kit", "celery", {"broker": broker})
    )


class TestCeleryTaskQueue(TaskQueueConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], TaskQueueProvider]:
        return lambda: _celery("memory://")

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], TaskQueueProvider]:
        # Nothing listens on port 1; the password is the kit's secret.
        return lambda: _celery(f"redis://:{self.leak_marker}@127.0.0.1:1/0")

    # A group that does not apply here; a ``None`` attribute is not collected (a
    # skip would be a test that runs on no CI lane: tools/ci/skip_ledger.py).
    # Spec 8.2: the unknown-task group is optional for a driver that cannot
    # reject at send: a producer that never booted the task table (an API
    # process) publishes any task name to the broker, and only the worker that
    # consumes it knows the table. The kernel rejects an undeclared task before
    # it reserves a key (kernel-layer tests: test_enqueue_deduplication).
    test_send_of_an_unknown_task_raises_invalid_argument = None  # type: ignore[assignment]


for _group in dir(TaskQueueConformance):
    if _group.startswith(_HOST_GROUP_PREFIXES):
        setattr(TestCeleryTaskQueue, _group, None)


class _Broken(TaskQueueProvider):
    """A driver with one planted defect, selected by ``defect``."""

    contract_version = 2
    supported_features = frozenset({"delay"})

    def __init__(self, defect: str) -> None:
        super().__init__(name="broken", driver="broken", options={})
        self.defect = defect

    def worker_host(self) -> Any:
        raise NotImplementedError

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
        if self.defect == "releases-on-failure":
            # The backend is down, yet the error says nothing was published.
            raise InvalidArgument("down", capability="taskqueue")
        if self.defect == "other-id":
            return uuid4().hex
        if self.defect == "accepts-anything":
            return task_id
        if self.defect == "unavailable-after-refusal":
            raise Unavailable("lost", capability="taskqueue")
        return task_id

    async def isolation_identity(self) -> str | None:
        return "broken:kit"


async def test_a_driver_that_releases_the_key_for_a_failing_backend_is_caught() -> None:
    kit = TaskQueueConformance()
    with pytest.raises(AssertionError, match="releases the idempotency key"):
        await kit.test_send_errors_carry_the_dedupe_decision(
            lambda: _Broken("releases-on-failure")
        )
    await kit.test_send_errors_carry_the_dedupe_decision(
        lambda: _Broken("unavailable-after-refusal")
    )


async def test_a_driver_that_answers_another_id_is_caught() -> None:
    kit = TaskQueueConformance()
    with pytest.raises(AssertionError, match="kernel chose the id"):
        await kit.test_send_returns_the_task_id_it_was_given(_Broken("other-id"))


async def test_a_driver_that_accepts_a_payload_that_is_not_json_is_caught() -> None:
    kit = TaskQueueConformance()
    with pytest.raises(pytest.fail.Exception):
        await kit.test_send_refuses_a_payload_that_is_not_a_json_object(
            _Broken("accepts-anything"), b"not json"
        )
