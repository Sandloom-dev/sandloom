"""The celery driver maps a broker failure to ``Unavailable`` / ``Timeout``
(spec 8.2): the publish may have happened, so the kernel keeps the dedupe key
pending either way."""

from __future__ import annotations

from uuid import uuid4

import pytest
from celery import Celery
from kombu.exceptions import OperationalError  # type: ignore[import-untyped]
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core.di import TaskEnqueueUncertain
from sandloom.core.infra.errors import Timeout, Unavailable
from sandloom.core.tasks import enqueue
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.core.testing import InMemoryCache
from sandloom.core.tests.test_taskqueue_enqueue_layer import register_probe_task


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    register_probe_task()
    app = Celery("mapping", broker="memory://", set_as_current=False)
    app.conf.task_default_queue = f"mapping-{uuid4().hex}"
    yield CeleryTaskQueueProvider(name="probe", driver="celery", options={}, app=app)
    app.close()


CASES = [
    (OperationalError("broker connection refused"), Unavailable),
    (ConnectionRefusedError("refused"), Unavailable),
    (RedisConnectionError("reset by peer"), Unavailable),
    (ValueError("I/O operation on closed file"), Unavailable),
    (ConnectionResetError("reset"), Unavailable),
    (TimeoutError("timed out"), Timeout),
    (RedisTimeoutError("read timed out"), Timeout),
]


def _failing(monkeypatch, provider, exc: Exception) -> None:
    def send_task(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(provider._app, "send_task", send_task)


@pytest.mark.parametrize(("exc", "mapped"), CASES)
async def test_a_broker_failure_is_mapped(monkeypatch, provider, exc, mapped) -> None:
    _failing(monkeypatch, provider, exc)
    with pytest.raises(mapped) as error:
        await provider.send("probe.task", b'{"x":1}', task_id="t-1")
    assert error.value.__cause__ is exc
    assert error.value.capability == "taskqueue"
    assert error.value.instance == "probe"
    assert error.value.retryable


@pytest.mark.parametrize(("exc", "mapped"), CASES)
async def test_a_mapped_failure_keeps_the_key_pending(
    monkeypatch, provider, exc, mapped
) -> None:
    _failing(monkeypatch, provider, exc)
    layered = KernelTaskQueue(provider)
    with pytest.raises(TaskEnqueueUncertain) as first:
        await layered.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert isinstance(first.value.__cause__, mapped)
    # The retry finds the key pending and never reaches the driver.
    with pytest.raises(TaskEnqueueUncertain) as retry:
        await layered.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert retry.value.__cause__ is None
