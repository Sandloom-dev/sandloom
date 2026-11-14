"""The kernel's enqueue layer above a taskqueue driver (spec 8.1, 8.2).

A refused enqueue (unknown task, a payload that does not validate, a delay the
driver cannot serve) leaves no dedupe key behind and sends nothing; the task id
is the kernel's; ``at_least_once`` and the encoded payload reach ``send``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.core.tasks import enqueue
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.core.tasks.registry import DeclaredTask, declared_task, register_task
from sandloom.core.testing import InMemoryCache


class ProbePayload(BaseModel):
    """Any JSON object: the probe task takes whatever a test sends."""

    model_config = ConfigDict(extra="allow")


class StrictPayload(BaseModel):
    amount: int


async def _probe(payload: ProbePayload) -> None:  # pragma: no cover - never run
    return None


async def _strict(payload: StrictPayload) -> None:  # pragma: no cover - never run
    return None


async def _retried(payload: ProbePayload) -> None:  # pragma: no cover - never run
    return None


def _declare(task: DeclaredTask) -> None:
    """Register ``task`` once (this module can be imported under two names)."""
    if declared_task(task.name) is None:
        register_task(task)


def register_probe_task() -> None:
    """Declare ``probe.task`` (any payload object) in this process's registry."""
    _declare(
        DeclaredTask(
            name="probe.task",
            function=_probe,
            payload_type=ProbePayload,
            queue="default",
            at_least_once=False,
            is_async=True,
        )
    )


@pytest.fixture(autouse=True)
def _declared_tasks() -> None:
    register_probe_task()
    _declare(
        DeclaredTask(
            name="probe.strict",
            function=_strict,
            payload_type=StrictPayload,
            queue="default",
            at_least_once=False,
            is_async=True,
        )
    )
    _declare(
        DeclaredTask(
            name="probe.retried",
            function=_retried,
            payload_type=ProbePayload,
            queue="default",
            at_least_once=True,
            is_async=True,
        )
    )


class RecordingProvider(TaskQueueProvider):
    """A driver that records every ``send`` and publishes nothing."""

    contract_version = 2
    supported_features = frozenset({"delay"})

    def __init__(self, *, features: frozenset[str] | None = None) -> None:
        super().__init__(name="probe", driver="recording", options={})
        self.sent: list[dict[str, Any]] = []
        if features is not None:
            self.features = lambda: features  # type: ignore[method-assign]

    async def send(
        self,
        task: str,
        payload: bytes,
        *,
        task_id: str,
        delay: timedelta | None = None,
        at_least_once: bool = False,
    ) -> str:
        self.sent.append(
            {
                "task": task,
                "payload": payload,
                "task_id": task_id,
                "delay": delay,
                "at_least_once": at_least_once,
            }
        )
        return task_id

    async def isolation_identity(self) -> str | None:
        return "recording:test"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    held = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: held)
    return held


async def test_send_gets_the_kernels_task_id_and_the_encoded_payload() -> None:
    provider = RecordingProvider()
    layer = KernelTaskQueue(provider)
    task_id = await layer.enqueue("probe.strict", {"amount": 5})
    [call] = provider.sent
    assert call["task_id"] == task_id and len(task_id) == 32
    assert call["payload"] == b'{"amount":5}'
    assert call["delay"] is None and call["at_least_once"] is False


async def test_a_keyed_enqueue_sends_under_the_reserved_id(
    store: InMemoryCache,
) -> None:
    provider = RecordingProvider()
    layer = KernelTaskQueue(provider)
    first = await layer.enqueue("probe.strict", {"amount": 5}, idempotency_key="k")
    assert (
        await layer.enqueue("probe.strict", {"amount": 5}, idempotency_key="k") == first
    )
    assert [c["task_id"] for c in provider.sent] == [first]


async def test_at_least_once_and_delay_reach_send() -> None:
    provider = RecordingProvider()
    layer = KernelTaskQueue(provider)
    await layer.enqueue("probe.retried", {}, delay=timedelta(seconds=30))
    [call] = provider.sent
    assert call["at_least_once"] is True
    assert call["delay"] == timedelta(seconds=30)


@pytest.mark.parametrize(
    ("task", "payload", "message"),
    [
        ("probe.nobody", {}, "not declared"),
        ("probe.strict", {"amount": "many"}, "refused"),
        ("probe.strict", {}, "refused"),
        ("probe.task", {"bad": object()}, "refused"),
    ],
)
async def test_a_refused_enqueue_leaves_no_key_and_sends_nothing(
    store: InMemoryCache, task: str, payload: dict[str, Any], message: str
) -> None:
    provider = RecordingProvider()
    layer = KernelTaskQueue(provider)
    with pytest.raises(InvalidArgument, match=message):
        await layer.enqueue(task, payload, idempotency_key="k")
    assert store.store == {}
    assert provider.sent == []


async def test_a_delay_without_the_feature_leaves_no_key(store: InMemoryCache) -> None:
    provider = RecordingProvider(features=frozenset())
    layer = KernelTaskQueue(provider)
    with pytest.raises(Unsupported) as refused:
        await layer.enqueue(
            "probe.task", {}, delay=timedelta(seconds=1), idempotency_key="k"
        )
    assert refused.value.feature == "delay"
    assert store.store == {}
    assert provider.sent == []


async def test_a_payload_for_a_task_without_one_is_refused(
    store: InMemoryCache,
) -> None:
    async def _bare() -> None:  # pragma: no cover - never run
        return None

    _declare(
        DeclaredTask(
            name="probe.bare",
            function=_bare,
            payload_type=None,
            queue="default",
            at_least_once=False,
            is_async=True,
        )
    )
    provider = RecordingProvider()
    layer = KernelTaskQueue(provider)
    with pytest.raises(InvalidArgument, match="refused"):
        await layer.enqueue("probe.bare", {"x": 1}, idempotency_key="k")
    assert store.store == {} and provider.sent == []
    await layer.enqueue("probe.bare", {})
    assert provider.sent[0]["payload"] == b"{}"


async def test_the_dedupe_namespace_follows_the_drivers_identity() -> None:
    one, other = RecordingProvider(), RecordingProvider()

    async def elsewhere() -> str:
        return "recording:other-broker"

    other.isolation_identity = elsewhere  # type: ignore[method-assign]
    assert await enqueue.dedupe_namespace(one) != await enqueue.dedupe_namespace(other)
