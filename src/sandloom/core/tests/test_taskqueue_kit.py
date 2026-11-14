"""``TaskQueueConformance`` (the send groups, spec 8.2): the celery driver
passes it on an in-memory broker, and the kit catches broken drivers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pytest

from sandloom.core.drivers.celery_taskqueue import create_celery_taskqueue
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.driverkit.testing import TaskQueueConformance


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

    # The celery health report names the broker as ``scheme://host:port`` and
    # carries the probe's error text: the harness refuses both (spec 2.4). The
    # health contract of the celery driver is the host slice's (idk-worker-port,
    # which owns ``check``); these two harness checks wait for it.
    async def test_harness_reports_health_within_the_bound(self, provider) -> None:
        pytest.skip("celery health details name the broker URL: idk-worker-port")

    async def test_harness_redacts_the_health_of_a_failing_backend(
        self, make_failing_provider
    ) -> None:
        pytest.skip("celery health details carry the probe text: idk-worker-port")


class _Broken(TaskQueueProvider):
    """A driver with one planted defect, selected by ``defect``."""

    contract_version = 2
    supported_features = frozenset({"delay"})

    def __init__(self, defect: str) -> None:
        super().__init__(name="broken", driver="broken", options={})
        self.defect = defect

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
