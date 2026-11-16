"""Enqueue dedupe (G-TASK-03) over the real celery driver: the kernel's layer
reserves the job id, the driver only sends, and the broker holds what was sent.

The layer's own transitions (reservation, replay, refusals before reserving) run
on a recording driver in ``sandloom.core.tests.test_enqueue_deduplication``; what
is here needs the Celery app: the messages on the broker, Celery's send errors
and the driver's refusals.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from celery import Celery
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core.di import TaskEnqueueUncertain
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.tasks import enqueue
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.core.testing import InMemoryCache
from sandloom.core.tests.test_taskqueue_enqueue_layer import register_probe_task


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    register_probe_task()
    # Not the process's current app: closing it deregisters it, and a closed
    # current app would swallow every later shared_task (NotRegistered).
    app = Celery("dedupe", broker="memory://", set_as_current=False)
    app.conf.task_default_queue = f"dedupe-{uuid4().hex}"
    provider = CeleryTaskQueueProvider(
        name="probe", driver="celery", options={}, app=app
    )
    yield _Layered(provider), app, store
    app.close()


class _Layered(KernelTaskQueue):
    """The kernel layer, with the provider's options reachable for a test."""

    __slots__ = ()

    @property
    def options(self) -> dict[str, object]:
        return self._provider.options  # type: ignore[return-value]


async def test_successful_retries_publish_one_real_message(queue) -> None:
    provider, app, _ = queue
    first = await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert (
        await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key") == first
    )
    with app.connection_for_read() as conn:
        messages = conn.SimpleQueue(app.conf.task_default_queue)
        assert messages.qsize() == 1
        message = messages.get(block=False)
        assert message.headers["id"] == first
        message.ack()


async def test_parallel_retries_never_publish_twice(queue) -> None:
    provider, app, _ = queue
    outcomes = await asyncio.gather(
        *(
            provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    ids = [result for result in outcomes if isinstance(result, str)]
    assert ids and len(set(ids)) == 1
    assert all(isinstance(result, (str, TaskEnqueueUncertain)) for result in outcomes)
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_broker_credential_rotation_preserves_deduplication(queue) -> None:
    provider, app, _ = queue
    provider.options["broker"] = "redis://:old-password@broker:6379/1"
    first = await provider.enqueue("probe.task", {}, idempotency_key="key")
    provider.options["broker"] = "redis://:new-password@broker:6379/1"
    assert await provider.enqueue("probe.task", {}, idempotency_key="key") == first
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_uncertain_broker_ack_is_not_republished(queue, monkeypatch) -> None:
    provider, app, _ = queue
    original = app.send_task

    def lose_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("ack lost after broker accepted the task")

    monkeypatch.setattr(app, "send_task", lose_ack)
    for _ in range(2):
        with pytest.raises(TaskEnqueueUncertain):
            await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_the_driver_refuses_a_payload_that_is_not_a_json_object(queue) -> None:
    layered, app, _ = queue
    provider = layered._provider
    for body in (b"not json", b"[1, 2]", b'"text"'):
        with pytest.raises(InvalidArgument):
            await provider.send("probe.task", body, task_id="t-1")
    with pytest.raises(InvalidArgument):
        await provider.send("", b"{}", task_id="t-1")
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 0


async def test_deterministic_send_error_releases_the_reservation(
    queue, monkeypatch
) -> None:
    from celery.exceptions import NotRegistered

    provider, app, _ = queue
    original = app.send_task

    def unknown(*args, **kwargs):
        raise NotRegistered("probe.task")

    monkeypatch.setattr(app, "send_task", unknown)
    with pytest.raises(InvalidArgument) as error:
        await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert isinstance(error.value.__cause__, NotRegistered)
    monkeypatch.setattr(app, "send_task", original)
    assert await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")


async def test_ambiguous_send_error_still_keeps_the_key_pending(
    queue, monkeypatch
) -> None:
    provider, app, _ = queue

    def ambiguous(*args, **kwargs):
        raise ValueError("I/O operation on closed file")  # mid-send: unknown

    monkeypatch.setattr(app, "send_task", ambiguous)
    for _ in range(2):
        with pytest.raises(TaskEnqueueUncertain):
            await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")


async def test_replay_window_expires_on_real_redis(
    queue, monkeypatch, real_cache: str
) -> None:
    from sandloom_redis.cache import RedisCacheProvider

    from sandloom.core.di import bind_tenant

    store = RedisCacheProvider(
        name="enqueue", driver="redis", options={"url": real_cache}, shared=False
    )
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    provider, app, _ = queue
    provider.options["dedupe_ttl_seconds"] = 1
    try:
        with bind_tenant(uuid4()):
            first = await provider.enqueue("probe.task", {}, idempotency_key="key")
            assert (
                await provider.enqueue("probe.task", {}, idempotency_key="key") == first
            )
            await asyncio.sleep(1.1)
            assert (
                await provider.enqueue("probe.task", {}, idempotency_key="key") != first
            )
        with app.connection_for_read() as conn:
            assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 2
    finally:
        await store.aclose()
